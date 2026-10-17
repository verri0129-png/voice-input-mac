import AVFoundation
import CoreAudio
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
        Transcriber.log("[rec] input format \(Int(formatIn.sampleRate))Hz \(formatIn.channelCount)ch device=\(inputDeviceName())")
        guard formatIn.channelCount > 0 else {
            Transcriber.log("[rec] 沒有可用的輸入裝置（channelCount=0）")
            return
        }
        // 取樣率轉換時保留通道數，之後才自己混成單聲道。
        // 直接請 AVAudioConverter 做 2ch → 1ch 會踩到 channel layout 問題：整段變成數位靜音
        // （本 app 一直是這樣，錄進來的每個檔都是全 0，Whisper 只好開始幻覺）。
        guard let formatOut = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate,
                                           channels: formatIn.channelCount, interleaved: false),
              let converter = AVAudioConverter(from: formatIn, to: formatOut) else {
            Transcriber.log("[rec] converter init failed（in=\(formatIn)）")
            return
        }
        let ratio = sampleRate / formatIn.sampleRate
        var loggedChannels = false
        input.installTap(onBus: 0, bufferSize: 4096, format: formatIn) { [weak self] buffer, _ in
            guard let self else { return }
            if !loggedChannels, let ch = buffer.floatChannelData {
                loggedChannels = true
                let peaks = (0..<Int(buffer.format.channelCount)).map { c -> String in
                    var p: Float = 0
                    for i in 0..<Int(buffer.frameLength) { p = max(p, abs(ch[c][i])) }
                    return String(format: "%.4f", p)
                }
                Transcriber.log("[rec] 硬體每通道峰值 ch=[\(peaks.joined(separator: ", "))] @\(Int(buffer.format.sampleRate))Hz")
            }
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
            guard let pcm = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: capacity) else { return }
            // 同一塊輸入只餵一次，之後回 .noDataNow；每次都回 .haveData 會讓 SRC 重複吃同一塊。
            var supplied = false
            var err: NSError?
            converter.convert(to: pcm, error: &err) { _, outStatus in
                if supplied { outStatus.pointee = .noDataNow; return nil }
                supplied = true
                outStatus.pointee = .haveData
                return buffer
            }
            if let err { Transcriber.log("[rec] convert err \(err)"); return }
            guard let ch = pcm.floatChannelData, pcm.frameLength > 0 else { return }
            let n = Int(pcm.frameLength)
            let chCount = Int(pcm.format.channelCount)
            if chCount == 1 {
                self.samples.append(contentsOf: UnsafeBufferPointer(start: ch[0], count: n))
            } else {
                for i in 0..<n {
                    var sum: Float = 0
                    for c in 0..<chCount { sum += ch[c][i] }
                    self.samples.append(sum / Float(chCount))
                }
            }
        }
        engine.prepare()
        do { try engine.start() } catch { Transcriber.log("[rec] engine start failed: \(error)") }
    }

    func stop() -> URL? {
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        guard !samples.isEmpty else {
            Transcriber.log("[rec] 沒有取到任何樣本（麥克風權限被拒，或輸入裝置沒送訊號）")
            return nil
        }
        let peak = samples.reduce(Float(0)) { max($0, abs($1)) }
        let peakDb = 20 * log10(Double(peak) + 1e-9)
        Transcriber.log("[rec] \(samples.count) samples (\(String(format: "%.1f", Double(samples.count) / sampleRate))s) peak \(String(format: "%.1f", peakDb)) dB")
        if peakDb < -60 {
            Transcriber.log("[rec] 警告：整段幾乎是靜音 —— whisper 只會產生幻覺文字")
        }
        // Write WAV 16k mono float32 -> int16
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("voice-\(Int(Date().timeIntervalSince1970)).wav")
        writeWAV(samples: samples, sampleRate: Int(sampleRate), url: url)
        return url
    }

    private func inputDeviceName() -> String {
        guard let unit = engine?.inputNode.audioUnit else { return "?" }
        var devID = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioUnitGetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &devID, &size) == noErr else { return "?" }
        var name: CFString = "" as CFString
        var nameSize = UInt32(MemoryLayout<CFString>.size)
        var addr = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectGetPropertyData(devID, &addr, 0, nil, &nameSize, &name) == noErr else { return "?" }
        return name as String
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
