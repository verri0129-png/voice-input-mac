import AVFoundation
import Foundation

final class Recorder: NSObject {
    private var engine: AVAudioEngine?
    private var samples: [Float] = []
    private let sampleRate: Double = 16000

    func start() {
        samples = []
        let engine = AVAudioEngine()
        self.engine = engine
        let input = engine.inputNode
        let formatIn = input.outputFormat(forBus: 0)
        guard let converter = AVAudioConverter(from: formatIn, to: AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)!) else {
            print("[rec] converter init failed"); return
        }
        // Use tap for simplicity (fallback to manual convert per buffer)
        input.installTap(onBus: 0, bufferSize: 4096, format: formatIn) { [weak self] buffer, _ in
            guard let self else { return }
            // Convert to 16k mono float32
            let pcm = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: AVAudioFrameCount(Double(buffer.frameLength) * self.sampleRate / formatIn.sampleRate))!
            var err: NSError?
            converter.convert(to: pcm, error: &err) { _, outStatus in
                outStatus.pointee = .haveData
                return buffer
            }
            if let err { print("[rec] convert err \(err)"); return }
            if let ch = pcm.floatChannelData?[0] {
                self.samples.append(contentsOf: Array(UnsafeBufferPointer(start: ch, count: Int(pcm.frameLength))))
            }
        }
        engine.prepare()
        do { try engine.start() } catch { print("[rec] engine start failed: \(error)") }
    }

    func stop() -> URL? {
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        guard !samples.isEmpty else { return nil }
        // Write WAV 16k mono float32 -> int16
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("voice-\(Int(Date().timeIntervalSince1970)).wav")
        writeWAV(samples: samples, sampleRate: Int(sampleRate), url: url)
        return url
    }

    private func writeWAV(samples: [Float], sampleRate: Int, url: URL) {
        var data = Data()
        let numSamples = samples.count
        let byteRate = sampleRate * 2
        func appendU32(_ v: UInt32) { var x = v.littleEndian; data.append(Data(bytes: &x, count: 4)) }
        func appendU16(_ v: UInt16) { var x = v.littleEndian; data.append(Data(bytes: &x, count: 2)) }
        data.append("RIFF".data(using: .ascii)!)
        appendU32(UInt32(36 + numSamples * 2))
        data.append("WAVE".data(using: .ascii)!)
        data.append("fmt ".data(using: .ascii)!)
        appendU32(16); appendU16(1); appendU16(1)
        appendU32(UInt32(sampleRate)); appendU32(UInt32(byteRate))
        appendU16(2); appendU16(16)
        data.append("data".data(using: .ascii)!)
        appendU32(UInt32(numSamples * 2))
        for s in samples {
            let v = Int16(max(-1, min(1, s)) * 32767)
            var le = v.littleEndian; data.append(Data(bytes: &le, count: 2))
        }
        try? data.write(to: url)
    }
}
