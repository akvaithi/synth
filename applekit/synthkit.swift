import Foundation
import EventKit

// Synth Apple layer.
//
// TCC attributes access requests to the *responsible process* — the ancestor that
// launched us — so a binary spawned from a shell can never hold Calendar/Reminders
// access. Only a LaunchServices- or launchd-launched process is its own responsible
// process. So the real mode of operation is `daemon`: launchd starts it, it holds the
// grants, and everything else talks to it over a Unix socket.
//
// Never deletes anything. Completion, not deletion.

let store = EKEventStore()
let iso: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

// MARK: - access

@discardableResult
func requestAccess(_ entity: EKEntityType) -> (Bool, String?) {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    var errMsg: String? = nil
    let handler: EKEventStoreRequestAccessCompletionHandler = { ok, err in
        granted = ok
        if let err = err { errMsg = err.localizedDescription }
        sem.signal()
    }
    if entity == .event {
        store.requestFullAccessToEvents(completion: handler)
    } else {
        store.requestFullAccessToReminders(completion: handler)
    }
    _ = sem.wait(timeout: .now() + 60)
    return (granted, errMsg)
}

func authString(_ s: EKAuthorizationStatus) -> String {
    switch s {
    case .notDetermined: return "notDetermined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .fullAccess: return "fullAccess"
    case .writeOnly: return "writeOnly"
    case .authorized: return "authorized"
    @unknown default: return "unknown"
    }
}

func calendarInfo(_ c: EKCalendar) -> [String: Any] {
    return [
        "id": c.calendarIdentifier,
        "title": c.title,
        "source": c.source?.title ?? "",
        "immutable": c.isImmutable,
        "allowsModify": c.allowsContentModifications,
    ]
}

// MARK: - reads

func listEvents(days: Int) -> [[String: Any]] {
    let start = Date()
    let end = Calendar.current.date(byAdding: .day, value: days, to: start)!
    let pred = store.predicateForEvents(withStart: start, end: end, calendars: nil)
    return store.events(matching: pred).map { e in
        [
            "id": e.eventIdentifier ?? "",
            "title": e.title ?? "",
            "start": e.startDate.map { iso.string(from: $0) } ?? "",
            "end": e.endDate.map { iso.string(from: $0) } ?? "",
            "allDay": e.isAllDay,
            "calendar": e.calendar?.title ?? "",
            "calendarId": e.calendar?.calendarIdentifier ?? "",
            "location": e.location ?? "",
            "notes": e.hasNotes ? (e.notes ?? "") : "",
        ]
    }
}

func listReminders(includeCompleted: Bool) -> [[String: Any]] {
    let sem = DispatchSemaphore(value: 0)
    var out: [[String: Any]] = []
    let pred = includeCompleted
        ? store.predicateForReminders(in: nil)
        : store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: nil)
    store.fetchReminders(matching: pred) { rems in
        for r in rems ?? [] {
            var due = ""
            if let dc = r.dueDateComponents, let d = Calendar.current.date(from: dc) {
                due = iso.string(from: d)
            }
            out.append([
                "id": r.calendarItemIdentifier,
                "externalId": r.calendarItemExternalIdentifier ?? "",
                "title": r.title ?? "",
                "list": r.calendar?.title ?? "",
                "listId": r.calendar?.calendarIdentifier ?? "",
                "due": due,
                "hasTime": r.dueDateComponents?.hour != nil,
                "completed": r.isCompleted,
                "completedAt": r.completionDate.map { iso.string(from: $0) } ?? "",
                "priority": r.priority,
                "notes": r.notes ?? "",
                "lastModified": r.lastModifiedDate.map { iso.string(from: $0) } ?? "",
            ])
        }
        sem.signal()
    }
    _ = sem.wait(timeout: .now() + 60)
    return out
}


// MARK: - writes
//
// Doctrine: never delete. Completion, not deletion. Partial updates only — a field the
// caller did not name is never cleared. Every write returns the prior state so the caller
// can record an undo entry.

func findReminderCalendar(_ nameOrId: String?) throws -> EKCalendar {
    let cals = store.calendars(for: .reminder)
    guard let want = nameOrId, !want.isEmpty else {
        guard let d = store.defaultCalendarForNewReminders() else {
            throw SynthError(msg: "no default reminder list")
        }
        return d
    }
    if let c = cals.first(where: { $0.calendarIdentifier == want || $0.title == want }) { return c }
    throw SynthError(msg: "no reminder list named \(want)")
}

func findEventCalendar(_ nameOrId: String?) throws -> EKCalendar {
    let cals = store.calendars(for: .event)
    guard let want = nameOrId, !want.isEmpty else {
        guard let d = store.defaultCalendarForNewEvents else {
            throw SynthError(msg: "no default calendar")
        }
        return d
    }
    if let c = cals.first(where: { $0.calendarIdentifier == want || $0.title == want }) { return c }
    throw SynthError(msg: "no calendar named \(want)")
}

func parseDate(_ s: String?) -> Date? {
    guard let s = s, !s.isEmpty else { return nil }
    if let d = iso.date(from: s) { return d }
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = f.date(from: s) { return d }
    let df = DateFormatter()
    df.dateFormat = "yyyy-MM-dd"
    df.timeZone = TimeZone.current
    return df.date(from: s)
}

func dueComponents(_ d: Date, hasTime: Bool) -> DateComponents {
    let cal = Calendar.current
    return hasTime
        ? cal.dateComponents([.year, .month, .day, .hour, .minute, .second], from: d)
        : cal.dateComponents([.year, .month, .day], from: d)
}

func fetchReminder(id: String) throws -> EKReminder {
    guard let item = store.calendarItem(withIdentifier: id) as? EKReminder else {
        throw SynthError(msg: "no reminder with identifier \(id)")
    }
    return item
}

func reminderSnapshot(_ r: EKReminder) -> [String: Any] {
    var due = ""
    if let dc = r.dueDateComponents, let d = Calendar.current.date(from: dc) {
        due = iso.string(from: d)
    }
    return [
        "id": r.calendarItemIdentifier,
        "title": r.title ?? "",
        "list": r.calendar?.title ?? "",
        "due": due,
        "hasTime": r.dueDateComponents?.hour != nil,
        "completed": r.isCompleted,
        "notes": r.notes ?? "",
        "priority": r.priority,
    ]
}

func eventSnapshot(_ e: EKEvent) -> [String: Any] {
    return [
        "id": e.eventIdentifier ?? "",
        "title": e.title ?? "",
        "start": e.startDate.map { iso.string(from: $0) } ?? "",
        "end": e.endDate.map { iso.string(from: $0) } ?? "",
        "allDay": e.isAllDay,
        "calendar": e.calendar?.title ?? "",
        "location": e.location ?? "",
        "notes": e.hasNotes ? (e.notes ?? "") : "",
    ]
}

func createReminder(_ req: [String: Any]) throws -> [String: Any] {
    guard let title = req["title"] as? String, !title.isEmpty else {
        throw SynthError(msg: "title is required")
    }
    let cal = try findReminderCalendar(req["list"] as? String)
    guard cal.allowsContentModifications else {
        throw SynthError(msg: "reminder list \(cal.title) is read-only")
    }
    let r = EKReminder(eventStore: store)
    r.title = title
    r.calendar = cal
    if let n = req["notes"] as? String, !n.isEmpty { r.notes = n }
    if let p = req["priority"] as? Int { r.priority = p }
    if let d = parseDate(req["due"] as? String) {
        let hasTime = (req["hasTime"] as? Bool) ?? ((req["due"] as? String)?.contains("T") ?? false)
        r.dueDateComponents = dueComponents(d, hasTime: hasTime)
        if hasTime {
            r.addAlarm(EKAlarm(absoluteDate: d))
        }
    }
    try store.save(r, commit: true)
    return ["created": reminderSnapshot(r), "before": NSNull()]
}

func completeReminder(_ req: [String: Any]) throws -> [String: Any] {
    guard let id = req["id"] as? String else { throw SynthError(msg: "id is required") }
    let r = try fetchReminder(id: id)
    let before = reminderSnapshot(r)
    if !r.isCompleted {
        r.isCompleted = true
        r.completionDate = Date()
        try store.save(r, commit: true)
    }
    return ["after": reminderSnapshot(r), "before": before]
}

func uncompleteReminder(_ req: [String: Any]) throws -> [String: Any] {
    guard let id = req["id"] as? String else { throw SynthError(msg: "id is required") }
    let r = try fetchReminder(id: id)
    let before = reminderSnapshot(r)
    r.isCompleted = false
    r.completionDate = nil
    try store.save(r, commit: true)
    return ["after": reminderSnapshot(r), "before": before]
}

func updateReminder(_ req: [String: Any]) throws -> [String: Any] {
    guard let id = req["id"] as? String else { throw SynthError(msg: "id is required") }
    let r = try fetchReminder(id: id)
    let before = reminderSnapshot(r)
    // Partial: only named fields are touched.
    if let t = req["title"] as? String { r.title = t }
    if let n = req["notes"] as? String { r.notes = n }
    if let p = req["priority"] as? Int { r.priority = p }
    if let dueStr = req["due"] as? String, let d = parseDate(dueStr) {
        let hasTime = (req["hasTime"] as? Bool) ?? dueStr.contains("T")
        r.dueDateComponents = dueComponents(d, hasTime: hasTime)
    }
    if let listName = req["list"] as? String {
        let cal = try findReminderCalendar(listName)
        guard cal.allowsContentModifications else {
            throw SynthError(msg: "reminder list \(cal.title) is read-only")
        }
        r.calendar = cal
    }
    try store.save(r, commit: true)
    return ["after": reminderSnapshot(r), "before": before]
}

func createEvent(_ req: [String: Any]) throws -> [String: Any] {
    guard let title = req["title"] as? String, !title.isEmpty else {
        throw SynthError(msg: "title is required")
    }
    guard let start = parseDate(req["start"] as? String) else {
        throw SynthError(msg: "start is required (ISO 8601)")
    }
    let cal = try findEventCalendar(req["calendar"] as? String)
    guard cal.allowsContentModifications else {
        throw SynthError(msg: "calendar \(cal.title) is read-only")
    }
    let e = EKEvent(eventStore: store)
    e.title = title
    e.calendar = cal
    e.startDate = start
    e.endDate = parseDate(req["end"] as? String)
        ?? Calendar.current.date(byAdding: .hour, value: 1, to: start)!
    if let allDay = req["allDay"] as? Bool { e.isAllDay = allDay }
    if let loc = req["location"] as? String, !loc.isEmpty { e.location = loc }
    if let n = req["notes"] as? String, !n.isEmpty { e.notes = n }
    try store.save(e, span: .thisEvent, commit: true)
    return ["created": eventSnapshot(e), "before": NSNull()]
}

func updateEvent(_ req: [String: Any]) throws -> [String: Any] {
    guard let id = req["id"] as? String else { throw SynthError(msg: "id is required") }
    guard let e = store.event(withIdentifier: id) else {
        throw SynthError(msg: "no event with identifier \(id)")
    }
    let before = eventSnapshot(e)
    if let t = req["title"] as? String { e.title = t }
    if let s = parseDate(req["start"] as? String) { e.startDate = s }
    if let en = parseDate(req["end"] as? String) { e.endDate = en }
    if let loc = req["location"] as? String { e.location = loc }
    if let n = req["notes"] as? String { e.notes = n }
    try store.save(e, span: .thisEvent, commit: true)
    return ["after": eventSnapshot(e), "before": before]
}


// MARK: - AppleScript / Notes
//
// Apple Events carry the same responsible-process rule as EventKit, so Notes and Mail are
// driven from inside this daemon rather than from a shell. Only named operations are exposed
// — there is deliberately no arbitrary-script passthrough on the socket.

let US = "\u{1F}"   // unit separator, between fields
let RS = "\u{1E}"   // record separator, between notes

func runAppleScript(_ src: String) throws -> String {
    var result: String = ""
    var thrown: String? = nil
    let work = {
        var errInfo: NSDictionary?
        guard let script = NSAppleScript(source: src) else {
            thrown = "could not compile AppleScript"
            return
        }
        let out = script.executeAndReturnError(&errInfo)
        if let e = errInfo {
            let msg = (e[NSAppleScript.errorMessage] as? String) ?? "\(e)"
            let num = (e[NSAppleScript.errorNumber] as? Int).map { " (\($0))" } ?? ""
            thrown = "applescript: \(msg)\(num)"
            return
        }
        result = out.stringValue ?? ""
    }
    if Thread.isMainThread { work() } else { DispatchQueue.main.sync(execute: work) }
    if let t = thrown { throw SynthError(msg: t) }
    return result
}

func asQuote(_ s: String) -> String {
    let escaped = s
        .replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
    return "\"\(escaped)\""
}

func notesFolders() throws -> [String] {
    let src = """
    tell application "Notes"
        set out to ""
        repeat with f in folders
            set out to out & (name of f) & (character id 30)
        end repeat
        return out
    end tell
    """
    return try runAppleScript(src)
        .components(separatedBy: RS)
        .filter { !$0.isEmpty }
}

func notesEnsureFolder(_ name: String) throws -> Bool {
    let existing = try notesFolders()
    if existing.contains(name) { return false }
    let src = """
    tell application "Notes"
        make new folder with properties {name:\(asQuote(name))}
    end tell
    """
    _ = try runAppleScript(src)
    return true
}

/// id, name and body for every note in a folder. Bodies are small and the caller hashes
/// them to detect edits, which is far more robust than parsing AppleScript's localised dates.
func notesDump(folder: String) throws -> [[String: Any]] {
    let src = """
    tell application "Notes"
        set out to ""
        repeat with n in notes of folder \(asQuote(folder))
            set out to out & (id of n) & (character id 31) & (name of n) & (character id 31) & (body of n) & (character id 30)
        end repeat
        return out
    end tell
    """
    let raw = try runAppleScript(src)
    var result: [[String: Any]] = []
    for rec in raw.components(separatedBy: RS) where !rec.isEmpty {
        let parts = rec.components(separatedBy: US)
        guard parts.count >= 3 else { continue }
        result.append([
            "id": parts[0],
            "name": parts[1],
            "body": parts[2...].joined(separator: US),
        ])
    }
    return result
}

func notesCreate(folder: String, name: String, body: String) throws -> [String: Any] {
    _ = try notesEnsureFolder(folder)
    let src = """
    tell application "Notes"
        set n to make new note at folder \(asQuote(folder)) with properties {name:\(asQuote(name)), body:\(asQuote(body))}
        return id of n
    end tell
    """
    let id = try runAppleScript(src)
    return ["id": id, "name": name, "folder": folder]
}

func notesUpdate(id: String, body: String, name: String?) throws -> [String: Any] {
    var setName = ""
    if let n = name { setName = "set name of theNote to \(asQuote(n))" }
    let src = """
    tell application "Notes"
        set theNote to note id \(asQuote(id))
        set oldBody to body of theNote
        set body of theNote to \(asQuote(body))
        \(setName)
        return oldBody
    end tell
    """
    let old = try runAppleScript(src)
    return ["id": id, "before": ["body": old]]
}

func notesGet(id: String) throws -> [String: Any] {
    let src = """
    tell application "Notes"
        set theNote to note id \(asQuote(id))
        return (name of theNote) & (character id 31) & (body of theNote)
    end tell
    """
    let raw = try runAppleScript(src)
    let parts = raw.components(separatedBy: US)
    return [
        "id": id,
        "name": parts.first ?? "",
        "body": parts.count > 1 ? parts[1...].joined(separator: US) : "",
    ]
}

// MARK: - dispatch

struct SynthError: Error { let msg: String }

func handle(_ req: [String: Any]) -> [String: Any] {
    let cmd = (req["cmd"] as? String) ?? "probe"
    do {
        switch cmd {
        case "ping":
            return ["ok": true, "result": ["pong": true, "pid": ProcessInfo.processInfo.processIdentifier]]
        case "probe":
            let (ev, evErr) = requestAccess(.event)
            let (rm, rmErr) = requestAccess(.reminder)
            return ["ok": true, "result": [
                "calendarGranted": ev,
                "calendarStatus": authString(EKEventStore.authorizationStatus(for: .event)),
                "calendarError": evErr ?? "",
                "remindersGranted": rm,
                "remindersStatus": authString(EKEventStore.authorizationStatus(for: .reminder)),
                "remindersError": rmErr ?? "",
                "calendars": store.calendars(for: .event).map { $0.title },
                "reminderLists": store.calendars(for: .reminder).map { $0.title },
            ]]
        case "calendars":
            return ["ok": true, "result": store.calendars(for: .event).map(calendarInfo)]
        case "lists":
            return ["ok": true, "result": store.calendars(for: .reminder).map(calendarInfo)]
        case "events":
            let days = (req["days"] as? Int) ?? 7
            return ["ok": true, "result": listEvents(days: days)]
        case "reminders":
            let inc = (req["includeCompleted"] as? Bool) ?? false
            return ["ok": true, "result": listReminders(includeCompleted: inc)]
        case "create_reminder":
            return ["ok": true, "result": try createReminder(req)]
        case "complete_reminder":
            return ["ok": true, "result": try completeReminder(req)]
        case "uncomplete_reminder":
            return ["ok": true, "result": try uncompleteReminder(req)]
        case "update_reminder":
            return ["ok": true, "result": try updateReminder(req)]
        case "create_event":
            return ["ok": true, "result": try createEvent(req)]
        case "update_event":
            return ["ok": true, "result": try updateEvent(req)]
        case "notes_folders":
            return ["ok": true, "result": try notesFolders()]
        case "notes_ensure_folder":
            guard let n = req["folder"] as? String else { throw SynthError(msg: "folder is required") }
            return ["ok": true, "result": ["created": try notesEnsureFolder(n)]]
        case "notes_dump":
            let f = (req["folder"] as? String) ?? "Synth"
            return ["ok": true, "result": try notesDump(folder: f)]
        case "notes_get":
            guard let id = req["id"] as? String else { throw SynthError(msg: "id is required") }
            return ["ok": true, "result": try notesGet(id: id)]
        case "notes_create":
            guard let name = req["name"] as? String, let body = req["body"] as? String else {
                throw SynthError(msg: "name and body are required")
            }
            return ["ok": true, "result": try notesCreate(folder: (req["folder"] as? String) ?? "Synth", name: name, body: body)]
        case "notes_update":
            guard let id = req["id"] as? String, let body = req["body"] as? String else {
                throw SynthError(msg: "id and body are required")
            }
            return ["ok": true, "result": try notesUpdate(id: id, body: body, name: req["name"] as? String)]
        default:
            throw SynthError(msg: "unknown command: \(cmd)")
        }
    } catch let e as SynthError {
        return ["ok": false, "error": e.msg]
    } catch {
        return ["ok": false, "error": "\(error)"]
    }
}

func encode(_ obj: [String: Any]) -> String {
    guard let d = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]),
          let s = String(data: d, encoding: .utf8) else {
        return "{\"ok\":false,\"error\":\"json encode failed\"}"
    }
    return s
}

// MARK: - change observer

final class ChangeObserver {
    let path: String
    init(queuePath: String) {
        self.path = queuePath
        NotificationCenter.default.addObserver(
            forName: .EKEventStoreChanged, object: store, queue: nil
        ) { [weak self] _ in self?.record() }
    }
    func record() {
        let line = encode([
            "at": iso.string(from: Date()),
            "kind": "eventkit_changed",
        ]) + "\n"
        guard let data = line.data(using: .utf8) else { return }
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
        if let fh = FileHandle(forWritingAtPath: path) {
            fh.seekToEndOfFile()
            fh.write(data)
            try? fh.close()
        }
    }
}

// MARK: - unix socket server

func serve(socketPath: String, queuePath: String) -> Never {
    unlink(socketPath)
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { FileHandle.standardError.write("socket() failed\n".data(using: .utf8)!); exit(1) }

    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let pathBytes = Array(socketPath.utf8)
    guard pathBytes.count < MemoryLayout.size(ofValue: addr.sun_path) else {
        FileHandle.standardError.write("socket path too long\n".data(using: .utf8)!); exit(1)
    }
    withUnsafeMutableBytes(of: &addr.sun_path) { raw in
        raw.copyBytes(from: pathBytes)
    }
    let addrLen = socklen_t(MemoryLayout<sockaddr_un>.size)
    let bindOK = withUnsafePointer(to: &addr) { p -> Bool in
        p.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
            Darwin.bind(fd, sa, addrLen) == 0
        }
    }
    guard bindOK else { FileHandle.standardError.write("bind() failed\n".data(using: .utf8)!); exit(1) }
    chmod(socketPath, 0o600)
    guard Darwin.listen(fd, 16) == 0 else {
        FileHandle.standardError.write("listen() failed\n".data(using: .utf8)!); exit(1)
    }

    // Warm the grants while we are still the responsible process.
    requestAccess(.event)
    requestAccess(.reminder)
    _ = ChangeObserver(queuePath: queuePath)

    let q = DispatchQueue(label: "page.akvaithi.synth.accept")
    q.async {
        while true {
            let client = Darwin.accept(fd, nil, nil)
            if client < 0 { continue }
            var buf = [UInt8](repeating: 0, count: 65536)
            let n = Darwin.read(client, &buf, buf.count)
            var response: [String: Any]
            if n <= 0 {
                response = ["ok": false, "error": "empty request"]
            } else {
                let data = Data(buf[0..<n])
                if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    response = handle(obj)
                } else {
                    response = ["ok": false, "error": "malformed json request"]
                }
            }
            var out = Array((encode(response) + "\n").utf8)
            _ = Darwin.write(client, &out, out.count)
            Darwin.close(client)
        }
    }
    RunLoop.main.run()
    exit(0)
}

// MARK: - entry

let args = Array(CommandLine.arguments.dropFirst())
func arg(_ name: String) -> String? {
    guard let i = args.firstIndex(of: "--\(name)"), i + 1 < args.count else { return nil }
    return args[i + 1]
}

if let i = args.firstIndex(of: "--out"), i + 1 < args.count {
    freopen(args[i + 1], "w", stdout)
}

let cmd = args.first ?? "probe"

if cmd == "daemon" {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    let sock = arg("socket") ?? "\(home)/Developer/synth/.state/synthd.sock"
    let queue = arg("queue") ?? "\(home)/Developer/synth/.state/changes.jsonl"
    serve(socketPath: sock, queuePath: queue)
} else {
    var req: [String: Any] = ["cmd": cmd]
    if let d = arg("days"), let n = Int(d) { req["days"] = n }
    if args.contains("--include-completed") { req["includeCompleted"] = true }
    print(encode(handle(req)))
}
