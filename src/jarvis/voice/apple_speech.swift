import Foundation
import Speech
import AVFoundation

// jarvis-speech: on-device Apple speech recognition of one audio file.
// usage: jarvis-speech <locale> <file> [contextual strings...]
// prints one JSON object: {"text": ..., "confidence": ..., "on_device": true}

@main
struct Main {
    static func main() async {
        let args = CommandLine.arguments
        guard args.count >= 3 else {
            FileHandle.standardError.write("usage: jarvis-speech <locale> <file> [hint ...]\n".data(using: .utf8)!)
            exit(2)
        }
        let locale = Locale(identifier: args[1])
        let url = URL(fileURLWithPath: args[2])
        let hints = Array(args.dropFirst(3))
        guard let recognizer = SFSpeechRecognizer(locale: locale) else {
            print("{\"error\": \"locale not supported\"}"); exit(1)
        }
        guard recognizer.supportsOnDeviceRecognition else {
            print("{\"error\": \"no on-device speech model for this locale (System Settings > Keyboard > Dictation)\"}"); exit(1)
        }
        var status = SFSpeechRecognizer.authorizationStatus()
        if status == .notDetermined {
            status = await withCheckedContinuation { (c: CheckedContinuation<SFSpeechRecognizerAuthorizationStatus, Never>) in
                SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0) }
            }
        }
        guard status == .authorized else {
            print("{\"error\": \"speech recognition not authorized (status \(status.rawValue))\"}"); exit(1)
        }
        let request = SFSpeechURLRecognitionRequest(url: url)
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = false
        if !hints.isEmpty { request.contextualStrings = hints }
        if #available(macOS 13.0, *) { request.addsPunctuation = true }
        let started = Date()
        let result: (String, Double)? = await withCheckedContinuation { (c: CheckedContinuation<(String, Double)?, Never>) in
            var done = false
            recognizer.recognitionTask(with: request) { result, error in
                if done { return }
                if let error = error {
                    done = true
                    FileHandle.standardError.write("\(error.localizedDescription)\n".data(using: .utf8)!)
                    c.resume(returning: nil); return
                }
                if let result = result, result.isFinal {
                    done = true
                    let segs = result.bestTranscription.segments
                    let conf = segs.isEmpty ? 0.0 : segs.map { Double($0.confidence) }.reduce(0, +) / Double(segs.count)
                    c.resume(returning: (result.bestTranscription.formattedString, conf))
                }
            }
        }
        let ms = Int(Date().timeIntervalSince(started) * 1000)
        guard let (text, conf) = result else { print("{\"error\": \"recognition failed\"}"); exit(1) }
        let obj: [String: Any] = ["text": text, "confidence": conf, "on_device": true, "ms": ms]
        let data = try! JSONSerialization.data(withJSONObject: obj)
        print(String(data: data, encoding: .utf8)!)
    }
}
