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
