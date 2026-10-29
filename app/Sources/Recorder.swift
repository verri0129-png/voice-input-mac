import AVFoundation
import CoreAudio
import Foundation

final class Recorder: NSObject {
    private var engine: AVAudioEngine?
    private var samples: [Float] = []
    /// 音訊執行緒 append、主執行緒／預覽執行緒讀 → 一律走這把鎖
    private let sampleLock = NSLock()
    private let sampleRate: Double = 16000
    /// 錄音中的即時音量 0…1（在主執行緒回呼），給畫面錄音指示圖用。
    var onLevel: ((Float) -> Void)?
    private var maxLevel: Float = 0
    private var loggedLevel = false

    /// 只收引擎、不動樣本（給 start() 開頭與 stop() 共用）。
    private func teardownEngine() {
        guard let engine else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        self.engine = nil
    }

    func start() {
        // 上一次沒收乾淨（引擎還在跑、tap 還掛在節點上）時再 install 一次 tap
        // 會讓 AVAudioEngine 丟 ObjC NSException，Swift 攔不到 → 整個 process SIGABRT。
        teardownEngine()
        sampleLock.lock(); samples = []; sampleLock.unlock()
        maxLevel = 0
        loggedLevel = false
        let engine = AVAudioEngine()
        self.engine = engine
        let input = engine.inputNode
        let formatIn = input.outputFormat(forBus: 0)
        Transcriber.log("[rec] input format \(Int(formatIn.sampleRate))Hz \(formatIn.channelCount)ch device=\(inputDeviceName())")
        // 沒有裝置、或系統塞了一個半死的 aggregate（實測 CADefaultDeviceAggregate）
        // 時 sampleRate 會是 0，硬 installTap 就是 abort。
        guard formatIn.sampleRate > 0, formatIn.channelCount > 0 else {
            Transcriber.log("[rec] 輸入裝置不可用（\(Int(formatIn.sampleRate))Hz \(formatIn.channelCount)ch），放棄這次錄音")
            teardownEngine()
            return
        }
        // 取樣率轉換時保留通道數，之後才自己混成單聲道。
        // 直接請 AVAudioConverter 做 2ch → 1ch 會踩到 channel layout 問題：整段變成數位靜音
        // （本 app 一直是這樣，錄進來的每個檔都是全 0，Whisper 只好開始幻覺）。
        // converter 改成拿到第一塊音訊後、依它的實際格式建：tap 的 format 傳 nil
        // （＝用節點當下的輸出格式），裝置中途被換掉也不會格式不符而炸掉。
        var converter: AVAudioConverter?
        var outFormat: AVAudioFormat?
        var ratio: Double = 1
        var loggedTapFormat = false
        var loggedChannels = false
        input.installTap(onBus: 0, bufferSize: 4096, format: nil) { [weak self] buffer, _ in
            guard let self else { return }
            if converter == nil {
                guard let out = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: self.sampleRate,
                                              channels: buffer.format.channelCount, interleaved: false),
                      let made = AVAudioConverter(from: buffer.format, to: out) else {
                    Transcriber.log("[rec] converter init failed（in=\(buffer.format)）")
                    return
                }
                converter = made
                outFormat = out
                ratio = self.sampleRate / buffer.format.sampleRate
            }
            if !loggedTapFormat {
                loggedTapFormat = true
                Transcriber.log("[rec] tap 實際格式 \(Int(buffer.format.sampleRate))Hz \(buffer.format.channelCount)ch")
            }
            if !loggedChannels, let ch = buffer.floatChannelData {
                loggedChannels = true
                let peaks = (0..<Int(buffer.format.channelCount)).map { c -> String in
                    var p: Float = 0
                    for i in 0..<Int(buffer.frameLength) { p = max(p, abs(ch[c][i])) }
                    return String(format: "%.4f", p)
                }
                Transcriber.log("[rec] 硬體每通道峰值 ch=[\(peaks.joined(separator: ", "))] @\(Int(buffer.format.sampleRate))Hz")
            }
            guard let converter, let outFormat else { return }
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
            guard let pcm = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
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
            // 即時音量（0…1，-60dB→0、0dB→1）給畫面指示圖。音訊執行緒上只抽樣算 RMS，運算在主執行緒。
            if let onLevel = self.onLevel {
                let step = max(1, n / 512)
                var acc: Float = 0
                var count = 0
                var i = 0
                while i < n {
                    // 跟下面混音邏輯一致：平均所有通道。這台裝置的訊號只出現在 ch[1]，
                    // 只看 ch[0] 會永遠是 0（實測 ch=[0.0000, 0.0096]）。
                    var sum: Float = 0
                    for c in 0..<chCount { sum += ch[c][i] }
                    let v = sum / Float(chCount)
                    acc += v * v; count += 1; i += step
                }
                let rms = (acc / Float(max(count, 1))).squareRoot()
                let db = 20 * log10(max(rms, 1e-7))
                // 靈敏度：這台裝置講話峰值約 -40dB，用 -55…-10dB 對應 0…1 比較看得出在動
                let norm = max(0, min(1, (db + 55) / 45))
                if !self.loggedLevel {
                    self.loggedLevel = true
                    Transcriber.log("[rec] 音波圖第一筆：rms=\(String(format: "%.5f", rms)) \(String(format: "%.1f", db))dB → \(String(format: "%.2f", norm))")
                }
                self.maxLevel = max(self.maxLevel, norm)
                DispatchQueue.main.async { onLevel(norm) }
            }
            if chCount == 1 {
                self.sampleLock.lock()
                self.samples.append(contentsOf: UnsafeBufferPointer(start: ch[0], count: n))
                self.sampleLock.unlock()
            } else {
                self.sampleLock.lock()
                for i in 0..<n {
                    var sum: Float = 0
                    for c in 0..<chCount { sum += ch[c][i] }
                    self.samples.append(sum / Float(chCount))
                }
                self.sampleLock.unlock()
            }
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            Transcriber.log("[rec] engine start failed: \(error)")
            teardownEngine()
        }
    }

    func stop() -> URL? {
        teardownEngine()
        let snapshot = snapshotSamples()
        guard !snapshot.isEmpty else {
            Transcriber.log("[rec] 沒有取到任何樣本（麥克風權限被拒，或輸入裝置沒送訊號）")
            return nil
        }
        let peak = snapshot.reduce(Float(0)) { max($0, abs($1)) }
        let peakDb = 20 * log10(Double(peak) + 1e-9)
        Transcriber.log("[rec] \(snapshot.count) samples (\(String(format: "%.1f", Double(snapshot.count) / sampleRate))s) peak \(String(format: "%.1f", peakDb)) dB（畫面音波圖峰值 \(String(format: "%.2f", maxLevel))）")
        if peakDb < -60 {
            Transcriber.log("[rec] 警告：整段幾乎是靜音 —— whisper 只會產生幻覺文字")
        }
        // Write WAV 16k mono float32 -> int16
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("voice-\(Int(Date().timeIntervalSince1970)).wav")
        writeWAV(samples: snapshot, sampleRate: Int(sampleRate), url: url)
        return url
    }

    /// 目前為止的音訊快照（錄音中即時預覽用；音訊執行緒同時在 append，所以一定要走鎖）。
    func snapshotSamples() -> [Float] {
        sampleLock.lock(); defer { sampleLock.unlock() }
        return samples
    }

    /// 把「目前為止」的音訊寫成獨立的 wav 檔給預覽用（不動正式結果用的緩衝）。
    /// 太短（<0.5s）回 false：這種長度 Whisper 只會吐幻覺，預覽也沒有意義。
    func writePartialWav(to url: URL) -> Bool {
        let snapshot = snapshotSamples()
        guard Double(snapshot.count) / sampleRate >= 0.5 else { return false }
        writeWAV(samples: snapshot, sampleRate: Int(sampleRate), url: url)
        return true
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
