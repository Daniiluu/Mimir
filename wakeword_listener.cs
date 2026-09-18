using System;
using System.Speech.Recognition;
using System.Threading;

class WakeWordListener {
    static void Main(string[] args) {
        try {
            using (SpeechRecognitionEngine engine = new SpeechRecognitionEngine()) {
                Choices keywords = new Choices();
                keywords.Add(new string[] { 
                    "Mimir", 
                    "Hey Mimir", 
                    "Oye Mimir", 
                    "Hola Mimir",
                    "Eh Mimir"
                });

                GrammarBuilder gb = new GrammarBuilder();
                gb.Append(keywords);

                Grammar grammar = new Grammar(gb);
                engine.LoadGrammar(grammar);

                engine.SetInputToDefaultAudioDevice();

                bool detected = false;
                engine.SpeechRecognized += (s, e) => {
                    if (!detected && e.Result.Confidence >= 0.5) {
                        detected = true;
                        Console.WriteLine("WAKEWORD_DETECTED:" + e.Result.Text);
                        Environment.Exit(0);
                    }
                };

                engine.RecognizeAsync(RecognizeMode.Multiple);

                while (!detected) {
                    Thread.Sleep(100);
                }
            }
        } catch (Exception ex) {
            Console.WriteLine("ERROR_WAKEWORD:" + ex.Message);
            Environment.Exit(1);
        }
    }
}
