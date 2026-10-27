import AppKit
import ApplicationServices
import Foundation

/// Typeless-style foreground context, captured immediately before transcription.
///
/// This only reads a tightly bounded text window around the focused input:
/// at most 100 characters before/after the cursor, at most 300 selected
/// characters, and at most 1,000 characters from visible labels. Browser URLs
/// have credentials, queries and fragments removed. Secure fields, password
/// managers and login/checkout pages return metadata only.
///
/// Raw values are never logged here; callers should log only field lengths.
enum ContextCapture {
    static let beforeLimit = 100
    static let afterLimit = 100
    static let selectedLimit = 300
    static let unrangedFieldLimit = 500
    static let maxFieldCharacters = 20_000
    static let fullFieldLimit = 3_000
    static let visibleCharacterLimit = 1_000
    static let visibleNodeLimit = 250
    static let visibleDepthLimit = 3

    private static let textRoles: Set<String> = [
        "AXTextArea", "AXTextField", "AXStaticText", "AXComboBox"
    ]
    private static let blockedRoles: Set<String> = [
        "AXSecureTextField"
    ]
    private static let sensitiveAppPrefixes = [
        "com.agilebits.onepassword",
        "com.bitwarden",
        "com.apple.keychainaccess"
    ]
    private static let sensitiveURLKeywords = [
        "login", "signin", "sign-in", "signup", "sign-up", "auth",
        "account", "password", "passkey", "checkout", "payment", "bank"
    ]

    private final class Box<T> {
        var value: T?
    }

    private struct Traversal {
        var nodes = 0
        var characters = 0
        var timeTruncated = false
        var nodeTruncated = false
    }

    static func captureJSON(deadlineMilliseconds: Int = 800) -> String? {
        let payload = capture(deadlineMilliseconds: deadlineMilliseconds)
        guard let data = try? JSONSerialization.data(
            withJSONObject: payload, options: [.sortedKeys]
        ) else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    static func encode(_ payload: [String: Any]) -> String? {
        guard let data = try? JSONSerialization.data(
            withJSONObject: payload, options: [.sortedKeys]
        ) else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    static func describe(_ payload: [String: Any]) -> String {
        func length(_ key: String) -> Int {
            (payload[key] as? String)?.count ?? 0
        }
        let app = payload["app_name"] as? String ?? "-"
        let role = payload["field_role"] as? String ?? "-"
        let blocked = (payload["blocked"] as? Bool) == true
        let reason = payload["reason"] as? String ?? "-"
        let ms = payload["capture_ms"] as? Int ?? -1
        let selected = payload["selected_text"] as? String
        return "app=\(app) role=\(role) before=\(length("text_before")) " +
            "after=\(length("text_after")) selected=\(selected?.count ?? 0) " +
            "visible=\(length("visible_text")) blocked=\(blocked) " +
            "reason=\(reason) capture_ms=\(ms)"
    }

    static func learningSnapshot(
        limit: Int = 5_000,
        deadlineMilliseconds: Int = 500
    ) -> [String: String]? {
        let deadline = Date().addingTimeInterval(Double(deadlineMilliseconds) / 1000.0)
        guard AXIsProcessTrusted(),
              let frontmost = NSWorkspace.shared.frontmostApplication,
              let bundleID = frontmost.bundleIdentifier,
              !(Bundle.main.bundleIdentifier.map { $0 == bundleID } ?? false),
              !isSensitiveApp(bundleID) else {
            return nil
        }
        let systemWide = AXUIElementCreateSystemWide()
        guard let appElement: AXUIElement = withDeadline(150, deadline: deadline, work: {
            copyElement(systemWide, kAXFocusedApplicationAttribute as CFString)
        }), let windowElement: AXUIElement = withDeadline(150, deadline: deadline, work: {
            copyElement(appElement, kAXFocusedWindowAttribute as CFString)
        }), let fieldElement: AXUIElement = withDeadline(150, deadline: deadline, work: {
            copyElement(appElement, kAXFocusedUIElementAttribute as CFString)
        }), let role: String = withDeadline(100, deadline: deadline, work: {
            copyAttribute(fieldElement, kAXRoleAttribute as CFString) as? String
        }), textRoles.contains(role), !blockedRoles.contains(role) else {
            return nil
        }
        let windowTitle = withDeadline(100, deadline: deadline, work: {
            stringValue(copyAttribute(windowElement, kAXTitleAttribute as CFString))
        }) ?? ""
        if let characters = withDeadline(100, deadline: deadline, work: {
            copyAttribute(fieldElement, kAXNumberOfCharactersAttribute as CFString) as? Int
        }), characters > limit {
            return nil
        }
        guard let value = withDeadline(250, deadline: deadline, work: {
            stringValue(copyAttribute(fieldElement, kAXValueAttribute as CFString))
        }), !value.isEmpty, value.count <= limit else {
            return nil
        }
        return [
            "bundle_id": bundleID,
            "window_title": windowTitle,
            "field_role": role,
            "field_text": value
        ]
    }

    /// Typeless 式定版用：回前景文字欄位的 element 與內容（與 learningSnapshot 同一套
    /// 解析，但保留 element，才能把剛貼上的那一段選起來改寫）。
    static func focusedTextField(
        deadlineMilliseconds: Int = 500
    ) -> (element: AXUIElement, text: String, role: String)? {
        let deadline = Date().addingTimeInterval(Double(deadlineMilliseconds) / 1000.0)
        guard AXIsProcessTrusted(),
              let frontmost = NSWorkspace.shared.frontmostApplication,
              let bundleID = frontmost.bundleIdentifier,
              !(Bundle.main.bundleIdentifier.map { $0 == bundleID } ?? false),
              !isSensitiveApp(bundleID) else {
            return nil
        }
        let systemWide = AXUIElementCreateSystemWide()
        guard let appElement: AXUIElement = withDeadline(150, deadline: deadline, work: {
            copyElement(systemWide, kAXFocusedApplicationAttribute as CFString)
        }), let fieldElement: AXUIElement = withDeadline(150, deadline: deadline, work: {
            copyElement(appElement, kAXFocusedUIElementAttribute as CFString)
        }), let role: String = withDeadline(100, deadline: deadline, work: {
            copyAttribute(fieldElement, kAXRoleAttribute as CFString) as? String
        }), textRoles.contains(role), !blockedRoles.contains(role) else {
            return nil
        }
        guard let value = withDeadline(250, deadline: deadline, work: {
            stringValue(copyAttribute(fieldElement, kAXValueAttribute as CFString))
        }) else {
            return nil
        }
        return (fieldElement, value, role)
    }

    /// 把焦點欄位的文字選取範圍移到 (location, length)；定版時要覆寫那一段。
    static func selectFieldRange(_ element: AXUIElement, location: Int, length: Int) -> Bool {
        var range = CFRange(location: location, length: length)
        guard let value = AXValueCreate(.cfRange, &range) else { return false }
        return AXUIElementSetAttributeValue(
            element,
            kAXSelectedTextRangeAttribute as CFString,
            value
        ) == .success
    }

    static func capture(deadlineMilliseconds: Int = 800) -> [String: Any] {
        let started = Date()
        let deadline = started.addingTimeInterval(Double(deadlineMilliseconds) / 1000.0)
        var payload: [String: Any] = ["source": "voice-input-mac"]
        guard AXIsProcessTrusted() else {
            return unavailable("accessibility-unavailable")
        }
        guard let frontmost = NSWorkspace.shared.frontmostApplication else {
            return unavailable("no-frontmost-app")
        }
        let bundleID = frontmost.bundleIdentifier ?? ""
        let appName = frontmost.localizedName ?? ""
        if let own = Bundle.main.bundleIdentifier, !own.isEmpty, own == bundleID {
            return unavailable("own-app")
        }
        payload["app_name"] = appName
        payload["bundle_id"] = bundleID
        if isSensitiveApp(bundleID) {
            payload["blocked"] = true
            payload["reason"] = "sensitive-app"
            return finish(payload, started: started)
        }

        let systemWide = AXUIElementCreateSystemWide()
        guard let appElement: AXUIElement = withDeadline(150, deadline: deadline, work: {
            copyElement(systemWide, kAXFocusedApplicationAttribute as CFString)
        }) else {
            payload["reason"] = "no-focused-application"
            return finish(payload, started: started)
        }

        var windowElement: AXUIElement?
        var fieldElement: AXUIElement?
        windowElement = withDeadline(150, deadline: deadline, work: {
            copyElement(appElement, kAXFocusedWindowAttribute as CFString)
        })
        fieldElement = withDeadline(150, deadline: deadline, work: {
            copyElement(appElement, kAXFocusedUIElementAttribute as CFString)
        })
        if fieldElement == nil {
            fieldElement = withDeadline(150, deadline: deadline, work: {
                copyElement(systemWide, kAXFocusedUIElementAttribute as CFString)
            })
        }

        if let windowElement {
            payload["window_title"] = withDeadline(120, deadline: deadline, work: {
                stringValue(copyAttribute(windowElement, kAXTitleAttribute as CFString))
            })
            if let url = withDeadline(200, deadline: deadline, work: {
                focusedURL(window: windowElement, field: fieldElement, deadline: deadline)
            }) {
                if isSensitiveURL(url) {
                    payload["blocked"] = true
                    payload["reason"] = "sensitive-url"
                    payload.removeValue(forKey: "app_name")
                    payload.removeValue(forKey: "bundle_id")
                    payload.removeValue(forKey: "window_title")
                    return finish(payload, started: started)
                }
                payload["web_url"] = url
            }
        }

        if let fieldElement {
            payload["field_role"] = withDeadline(100, deadline: deadline, work: {
                copyAttribute(fieldElement, kAXRoleAttribute as CFString) as? String
            })
            if let role = payload["field_role"] as? String, blockedRoles.contains(role) {
                payload["blocked"] = true
                payload["reason"] = "secure-field"
                payload.removeValue(forKey: "window_title")
                payload.removeValue(forKey: "web_url")
                return finish(payload, started: started)
            }
            if let selected = withDeadline(150, deadline: deadline, work: {
                selectedText(in: fieldElement)
            }), !selected.isEmpty {
                payload["selected_text"] = String(selected.prefix(selectedLimit))
                payload["selected_truncated"] = selected.count > selectedLimit
            }
            if let snapshot = withDeadline(250, deadline: deadline, work: {
                fieldSnapshot(in: fieldElement, role: payload["field_role"] as? String)
            }) {
                for (key, value) in snapshot where !value.isEmpty {
                    payload[key] = value
                }
            }
        }

        if let windowElement {
            var traversal = Traversal()
            var seen = Set<UnsafeRawPointer>()
            var texts: [String] = []
            collectVisibleText(
                from: windowElement,
                depth: 0,
                deadline: deadline,
                traversal: &traversal,
                seen: &seen,
                texts: &texts
            )
            let joined = collapseWhitespace(texts.joined(separator: " "))
            if !joined.isEmpty {
                payload["visible_text"] = String(joined.prefix(visibleCharacterLimit))
                payload["visible_truncated"] =
                    joined.count > visibleCharacterLimit ||
                    traversal.timeTruncated || traversal.nodeTruncated
            }
        }
        return finish(payload, started: started)
    }

    // MARK: - AX helpers

    private static func unavailable(_ reason: String) -> [String: Any] {
        [
            "source": "voice-input-mac",
            "blocked": true,
            "reason": reason,
            "captured_at": ISO8601DateFormatter().string(from: Date())
        ]
    }

    private static func finish(_ payload: [String: Any], started: Date) -> [String: Any] {
        var payload = payload
        payload["captured_at"] = ISO8601DateFormatter().string(from: Date())
        payload["capture_ms"] = Int(Date().timeIntervalSince(started) * 1000)
        return payload
    }

    private static func withDeadline<T>(
        _ milliseconds: Int,
        deadline: Date,
        work: @escaping () -> T?
    ) -> T? {
        if Date() >= deadline { return nil }
        let box = Box<T>()
        let group = DispatchGroup()
        group.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            box.value = work()
            group.leave()
        }
        let remaining = max(1, Int(deadline.timeIntervalSinceNow * 1000))
        let timeout = min(milliseconds, remaining)
        return group.wait(timeout: .now() + .milliseconds(timeout)) == .success ? box.value : nil
    }

    private static func copyAttribute(_ element: AXUIElement, _ name: CFString) -> AnyObject? {
        var value: AnyObject?
        guard AXUIElementCopyAttributeValue(element, name, &value) == .success else {
            return nil
        }
        return value
    }

    private static func copyElement(_ element: AXUIElement, _ name: CFString) -> AXUIElement? {
        guard let value = copyAttribute(element, name) else { return nil }
        return value as! AXUIElement
    }

    private static func stringValue(_ value: AnyObject?) -> String? {
        if let text = value as? String, !text.isEmpty { return text }
        if let attributed = value as? NSAttributedString, !attributed.string.isEmpty {
            return attributed.string
        }
        return nil
    }

    private static func selectedText(in element: AXUIElement) -> String? {
        stringValue(copyAttribute(element, kAXSelectedTextAttribute as CFString))
    }

    private static func selectedRange(in element: AXUIElement) -> CFRange? {
        guard let raw = copyAttribute(element, kAXSelectedTextRangeAttribute as CFString),
              CFGetTypeID(raw as CFTypeRef) == AXValueGetTypeID() else {
            return nil
        }
        let value = raw as! AXValue
        guard AXValueGetType(value) == .cfRange else { return nil }
        var range = CFRange(location: 0, length: 0)
        guard AXValueGetValue(value, .cfRange, &range) else { return nil }
        return range
    }

    private static func fieldSnapshot(
        in element: AXUIElement,
        role: String?
    ) -> [String: String] {
        guard let role, textRoles.contains(role) else { return [:] }
        if let characters = copyAttribute(element, kAXNumberOfCharactersAttribute as CFString) as? Int,
           characters > maxFieldCharacters {
             return [:]
        }
        guard let full = stringValue(copyAttribute(element, kAXValueAttribute as CFString)) else {
            return [:]
        }
        let ns = full as NSString
        if ns.length > maxFieldCharacters { return [:] }
        if let range = selectedRange(in: element) {
            let location = max(0, min(range.location, ns.length))
            let length = max(0, min(range.length, ns.length - location))
            let beforeStart = max(0, location - beforeLimit)
            let afterEnd = min(ns.length, location + length + afterLimit)
            // 整份輸入框內容：Typeless 的做法（他們的 `full_field_content`）。
            // 同音字光靠聲音分不出來，欄位裡已經出現過的寫法才是最強的線索。
            let fieldStart = max(0, location + length - fullFieldLimit)
            return [
                "text_before": ns.substring(with: NSRange(location: beforeStart, length: location - beforeStart)),
                "text_after": ns.substring(with: NSRange(location: location + length, length: afterEnd - location - length)),
                "cursor_position": String(location),
                "full_field_text": ns.substring(with: NSRange(location: fieldStart, length: location + length - fieldStart))
            ]
        }
        if ns.length <= unrangedFieldLimit {
            return ["field_text": ns.substring(to: ns.length)]
        }
        return [:]
    }

    private static func focusedURL(
        window: AXUIElement,
        field: AXUIElement?,
        deadline: Date
    ) -> String? {
        var candidates: [AnyObject?] = [
            copyAttribute(window, kAXDocumentAttribute as CFString),
            copyAttribute(window, kAXURLAttribute as CFString)
        ]
        if let field {
            candidates.append(copyAttribute(field, kAXURLAttribute as CFString))
            candidates.append(copyAttribute(field, kAXDocumentAttribute as CFString))
            var cursor: AXUIElement? = field
            var seen = Set<UnsafeRawPointer>()
            for _ in 0..<8 {
                guard let current = cursor else { break }
                let key = Unmanaged.passUnretained(current).toOpaque()
                guard seen.insert(key).inserted else { break }
                candidates.append(copyAttribute(current, kAXURLAttribute as CFString))
                candidates.append(copyAttribute(current, kAXDocumentAttribute as CFString))
                if let parent = copyAttribute(current, kAXParentAttribute as CFString) {
                    cursor = parent as! AXUIElement
                } else {
                    cursor = nil
                }
                if Date() >= deadline { break }
            }
        }
        for candidate in candidates {
            if let text = stringValue(candidate), let url = firstURL(in: text) {
                return url
            }
        }
        return findAddressBarURL(in: window, deadline: deadline)
    }

    private static func findAddressBarURL(in root: AXUIElement, deadline: Date) -> String? {
        var queue: [(AXUIElement, Int)] = [(root, 0)]
        var seen = Set<UnsafeRawPointer>()
        var nodes = 0
        while !queue.isEmpty {
            if Date() >= deadline || nodes >= visibleNodeLimit { return nil }
            let (element, depth) = queue.removeFirst()
            let key = Unmanaged.passUnretained(element).toOpaque()
            guard seen.insert(key).inserted else { continue }
            nodes += 1
            if depth <= visibleDepthLimit,
               let role = copyAttribute(element, kAXRoleAttribute as CFString) as? String,
               role == "AXTextField",
               let text = stringValue(copyAttribute(element, kAXValueAttribute as CFString)),
               let url = firstURL(in: text) {
                return url
            }
            if depth >= visibleDepthLimit { continue }
            guard let children = copyAttribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] else {
                continue
            }
            for child in children.prefix(50) {
                queue.append((child, depth + 1))
            }
        }
        return nil
    }

    private static func firstURL(in text: String) -> String? {
        guard let match = text.range(
            of: #"(https?://|file://)[^\s"'<>]+"#,
            options: .regularExpression
        ) else {
            return nil
        }
        var candidate = String(text[match]).trimmingCharacters(in: .whitespacesAndNewlines)
        while let last = candidate.last, ".,;)]}'\"".contains(last) {
            candidate.removeLast()
        }
        return sanitizeURL(candidate)
    }

    private static func sanitizeURL(_ candidate: String) -> String? {
        guard var components = URLComponents(string: candidate),
              let scheme = components.scheme?.lowercased(),
              ["http", "https", "file"].contains(scheme),
              components.host != nil || scheme == "file" else {
            return nil
        }
        components.user = nil
        components.password = nil
        components.query = nil
        components.fragment = nil
        return components.string
    }

    private static func collectVisibleText(
        from element: AXUIElement,
        depth: Int,
        deadline: Date,
        traversal: inout Traversal,
        seen: inout Set<UnsafeRawPointer>,
        texts: inout [String]
    ) {
        if Date() >= deadline || traversal.nodes >= visibleNodeLimit {
            traversal.timeTruncated = Date() >= deadline
            traversal.nodeTruncated = traversal.nodes >= visibleNodeLimit
            return
        }
        let key = Unmanaged.passUnretained(element).toOpaque()
        guard seen.insert(key).inserted else { return }
        traversal.nodes += 1
        let role = copyAttribute(element, kAXRoleAttribute as CFString) as? String ?? ""
        if blockedRoles.contains(role) { return }
        if textRoles.contains(role),
           let text = stringValue(copyAttribute(element, kAXValueAttribute as CFString)) {
            appendVisibleText(text, traversal: &traversal, texts: &texts)
            if traversal.characters >= visibleCharacterLimit { return }
        }
        if depth >= visibleDepthLimit { return }
        guard let children = copyAttribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] else {
            return
        }
        for child in children {
            collectVisibleText(
                from: child,
                depth: depth + 1,
                deadline: deadline,
                traversal: &traversal,
                seen: &seen,
                texts: &texts
            )
            if traversal.characters >= visibleCharacterLimit { return }
            if Date() >= deadline { return }
        }
    }

    private static func appendVisibleText(
        _ text: String,
        traversal: inout Traversal,
        texts: inout [String]
    ) {
        let cleaned = collapseWhitespace(text)
        guard !cleaned.isEmpty else { return }
        let remaining = visibleCharacterLimit - traversal.characters
        guard remaining > 0 else { return }
        texts.append(String(cleaned.prefix(remaining)))
        traversal.characters += min(cleaned.count, remaining)
    }

    private static func collapseWhitespace(_ text: String) -> String {
        text.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func isSensitiveApp(_ bundleID: String) -> Bool {
        let id = bundleID.lowercased()
        return sensitiveAppPrefixes.contains { id.hasPrefix($0) }
    }

    private static func isSensitiveURL(_ url: String) -> Bool {
        let lower = url.lowercased()
        return sensitiveURLKeywords.contains { lower.contains($0) }
    }
}
