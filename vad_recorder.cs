using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

class VADRecorder {
    [DllImport("winmm.dll")]
    public static extern int waveInOpen(out IntPtr phwi, int uDeviceID, ref WAVEFORMATEX pwfx, IntPtr dwCallback, IntPtr dwInstance, int fdwOpen);
    [DllImport("winmm.dll")]
    public static extern int waveInStart(IntPtr hwi);
    [DllImport("winmm.dll")]
    public static extern int waveInStop(IntPtr hwi);
    [DllImport("winmm.dll")]
    public static extern int waveInReset(IntPtr hwi);
    [DllImport("winmm.dll")]
    public static extern int waveInClose(IntPtr hwi);
    [DllImport("winmm.dll")]
    public static extern int waveInPrepareHeader(IntPtr hwi, ref WAVEHDR pwh, int cbwh);
    [DllImport("winmm.dll")]
    public static extern int waveInUnprepareHeader(IntPtr hwi, ref WAVEHDR pwh, int cbwh);
    [DllImport("winmm.dll")]
    public static extern int waveInAddBuffer(IntPtr hwi, ref WAVEHDR pwh, int cbwh);

    [StructLayout(LayoutKind.Sequential)]
    public struct WAVEFORMATEX {
        public short wFormatTag;
        public short nChannels;
        public int nSamplesPerSec;
        public int nAvgBytesPerSec;
        public short nBlockAlign;
        public short wBitsPerSample;
        public short cbSize;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct WAVEHDR {
        public IntPtr lpData;
        public int dwBufferLength;
        public int dwBytesRecorded;
        public IntPtr dwUser;
        public int dwFlags;
        public int dwLoops;
        public IntPtr lpNext;
        public IntPtr reserved;
    }

    static void Main(string[] args) {
        string outputFile = args.Length > 0 ? args[0] : "temp_entrada.wav";
        int maxSilenceBeforeSpeechSec = args.Length > 1 ? int.Parse(args[1]) : 8;

        WAVEFORMATEX fmt = new WAVEFORMATEX();
        fmt.wFormatTag = 1; // PCM
        fmt.nChannels = 1;  // Mono
        fmt.nSamplesPerSec = 16000;
        fmt.wBitsPerSample = 16;
        fmt.nBlockAlign = (short)(fmt.nChannels * (fmt.wBitsPerSample / 8));
        fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign;
        fmt.cbSize = 0;

        IntPtr hWaveIn;
        int res = waveInOpen(out hWaveIn, -1, ref fmt, IntPtr.Zero, IntPtr.Zero, 0);
        if (res != 0) {
            Console.WriteLine("Error al abrir micrófono.");
            Environment.Exit(1);
        }

        const int bufferSize = 3200; // 100ms at 16kHz 16-bit mono
        const int numBuffers = 4;
        IntPtr[] buffers = new IntPtr[numBuffers];
        WAVEHDR[] headers = new WAVEHDR[numBuffers];

        for (int i = 0; i < numBuffers; i++) {
            buffers[i] = Marshal.AllocHGlobal(bufferSize);
            headers[i] = new WAVEHDR {
                lpData = buffers[i],
                dwBufferLength = bufferSize
            };
            waveInPrepareHeader(hWaveIn, ref headers[i], Marshal.SizeOf(typeof(WAVEHDR)));
            waveInAddBuffer(hWaveIn, ref headers[i], Marshal.SizeOf(typeof(WAVEHDR)));
        }

        waveInStart(hWaveIn);

        MemoryStream audioMs = new MemoryStream();
        bool speechStarted = false;
        int silenceMsCounter = 0;
        int initialWaitMsCounter = 0;
        int totalSpeechMsCounter = 0;
        const int silenceThresholdRMS = 300; // Sensibilidad del micrófono
        const int silenceDurationToStopMs = 1200; // 1.2s de silencio tras hablar para finalizar
        const int maxTotalSpeechMs = 30000; // Máximo 30s de grabación

        while (true) {
            Thread.Sleep(100);
            initialWaitMsCounter += 100;

            for (int i = 0; i < numBuffers; i++) {
                if ((headers[i].dwFlags & 1) != 0) { // WHDR_DONE = 1
                    int bytesRecorded = headers[i].dwBytesRecorded;
                    if (bytesRecorded > 0) {
                        byte[] rawBytes = new byte[bytesRecorded];
                        Marshal.Copy(headers[i].lpData, rawBytes, 0, bytesRecorded);

                        double sumSq = 0;
                        int samples = bytesRecorded / 2;
                        for (int s = 0; s < samples; s++) {
                            short sample = BitConverter.ToInt16(rawBytes, s * 2);
                            sumSq += sample * sample;
                        }
                        double rms = Math.Sqrt(sumSq / samples);

                        if (rms > silenceThresholdRMS) {
                            if (!speechStarted) {
                                speechStarted = true;
                                Console.WriteLine("🎙️ [Voz Detectada - Grabando...]");
                            }
                            silenceMsCounter = 0;
                        } else {
                            if (speechStarted) {
                                silenceMsCounter += 100;
                            }
                        }

                        if (speechStarted) {
                            audioMs.Write(rawBytes, 0, bytesRecorded);
                            totalSpeechMsCounter += 100;
                        }
                    }

                    waveInUnprepareHeader(hWaveIn, ref headers[i], Marshal.SizeOf(typeof(WAVEHDR)));
                    headers[i].dwFlags = 0;
                    waveInPrepareHeader(hWaveIn, ref headers[i], Marshal.SizeOf(typeof(WAVEHDR)));
                    waveInAddBuffer(hWaveIn, ref headers[i], Marshal.SizeOf(typeof(WAVEHDR)));
                }
            }

            if (speechStarted && silenceMsCounter >= silenceDurationToStopMs) {
                break;
            }
            if (!speechStarted && initialWaitMsCounter >= maxSilenceBeforeSpeechSec * 1000) {
                break;
            }
            if (speechStarted && totalSpeechMsCounter >= maxTotalSpeechMs) {
                break;
            }
        }

        waveInStop(hWaveIn);
        waveInReset(hWaveIn);
        for (int i = 0; i < numBuffers; i++) {
            waveInUnprepareHeader(hWaveIn, ref headers[i], Marshal.SizeOf(typeof(WAVEHDR)));
            Marshal.FreeHGlobal(buffers[i]);
        }
        waveInClose(hWaveIn);

        if (speechStarted && audioMs.Length > 0) {
            byte[] pcmData = audioMs.ToArray();
            using (FileStream fs = new FileStream(outputFile, FileMode.Create))
            using (BinaryWriter bw = new BinaryWriter(fs)) {
                bw.Write(new char[] { 'R', 'I', 'F', 'F' });
                bw.Write(36 + pcmData.Length);
                bw.Write(new char[] { 'W', 'A', 'V', 'E' });
                bw.Write(new char[] { 'f', 'm', 't', ' ' });
                bw.Write(16);
                bw.Write((short)1);
                bw.Write(fmt.nChannels);
                bw.Write(fmt.nSamplesPerSec);
                bw.Write(fmt.nAvgBytesPerSec);
                bw.Write(fmt.nBlockAlign);
                bw.Write(fmt.wBitsPerSample);
                bw.Write(new char[] { 'd', 'a', 't', 'a' });
                bw.Write(pcmData.Length);
                bw.Write(pcmData);
            }
            Console.WriteLine("✅ Grabación finalizada (Silencio detectado).");
            Environment.Exit(0);
        } else {
            Environment.Exit(2);
        }
    }
}
