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

func listEvents(days: Int, from: Date? = nil, to: Date? = nil) -> [[String: Any]] {
    let start = from ?? Date()
    let end = to ?? Calendar.current.date(byAdding: .day, value: days, to: start)!
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

/// Removes a reminder. Deliberately the only deletion in the whole Apple layer, and the
/// Python side refuses to call it unless action_log proves Synth created the reminder itself.
/// Cleaning up its own mistakes is Synth's job, not Arun's; deleting anything of his is not.
func deleteReminder(_ req: [String: Any]) throws -> [String: Any] {
    guard let id = req["id"] as? String else { throw SynthError(msg: "id is required") }
    let r = try fetchReminder(id: id)
    let before = reminderSnapshot(r)
    try store.remove(r, commit: true)
    return ["removed": true, "before": before]
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



func epochToISO(_ s: String) -> String {
    guard let secs = Double(s.trimmingCharacters(in: .whitespacesAndNewlines)) else { return s }
    return iso.string(from: Date(timeIntervalSince1970: secs))
}

// MARK: - Mail
//
// Read-only plus draft creation. There is deliberately no send path, and none may be added:
// a sent message cannot be recalled and would be attributed to Arun. Drafting is the maximum
// write that is ever acceptable here.

func mailAccounts() throws -> [[String: Any]] {
    let src = """
    tell application "Mail"
        set out to ""
        repeat with a in accounts
            set addrs to ""
            try
                set addrs to (email addresses of a) as string
            end try
            set out to out & (name of a) & (character id 31) & addrs & (character id 31) & ((enabled of a) as string) & (character id 30)
        end repeat
        return out
    end tell
    """
    let raw = try runAppleScript(src)
    var out: [[String: Any]] = []
    for rec in raw.components(separatedBy: RS) where !rec.isEmpty {
        let f = rec.components(separatedBy: US)
        guard f.count >= 3 else { continue }
        out.append(["name": f[0], "addresses": f[1], "enabled": f[2] == "true"])
    }
    return out
}

/// Resolves a message by (account, mailbox, index) and verifies its Message-ID matches
/// what the caller expected. Indices shift as mail arrives, so the id is the safety check —
/// but addressing by index is the difference between milliseconds and minutes, because
/// `whose message id is ...` walks the entire mailbox.
func messageRef(account: String, mailbox: String, index: Int) -> String {
    return """
    set acct to first account whose name is \(asQuote(account))
    set box to missing value
    repeat with mb in mailboxes of acct
        set nm to name of mb
        if nm is \(asQuote(mailbox)) or nm ends with ("/" & \(asQuote(mailbox))) then
            set box to mb
            exit repeat
        end if
    end repeat
    if box is missing value then error "no mailbox named " & \(asQuote(mailbox))
    set m to message \(index) of box
    """
}

/// Raw RFC822 source of one message.
///
/// `content of message` returns Mail's plain-text rendering, which drops every hyperlink —
/// so an opportunity email arrives with the prose but not the URL, which is exactly the
/// friction Synth exists to remove. The raw source keeps the HTML part, and the hrefs with it.
func mailSource(account: String, mailbox: String, index: Int, expectId: String,
                maxBytes: Int) throws -> [String: Any] {
    let src = """
    tell application "Mail"
        \(messageRef(account: account, mailbox: mailbox, index: index))
        set actualId to message id of m
        if actualId is not \(asQuote(expectId)) then return "MISMATCH"
        with timeout of 600 seconds
            set s to source of m
        end timeout
        return s
    end tell
    """
    let raw = try runAppleScript(src)
    if raw == "MISMATCH" { throw SynthError(msg: "index no longer points at \(expectId)") }
    let truncated = raw.count > maxBytes
    return [
        "messageId": expectId,
        "source": truncated ? String(raw.prefix(maxBytes)) : raw,
        "truncated": truncated,
        "bytes": raw.count,
    ]
}

func mailAttachments(account: String, mailbox: String, index: Int, expectId: String) throws -> [String: Any] {
    let src = """
    tell application "Mail"
        \(messageRef(account: account, mailbox: mailbox, index: index))
        set actualId to message id of m
        if actualId is not \(asQuote(expectId)) then return "MISMATCH" & (character id 31) & actualId
        set out to "OK" & (character id 30)
        -- Every property is guarded: Mail raises -10000 on some attachments (inline parts,
        -- undownloaded items) rather than returning a value, and one bad attachment must
        -- not lose the whole message.
        try
            repeat with att in mail attachments of m
                set aname to "(unnamed)"
                try
                    set aname to (name of att) as string
                end try
                set amime to "application/octet-stream"
                try
                    set amime to (MIME type of att) as string
                end try
                set sz to "0"
                try
                    set sz to ((file size of att) as string)
                end try
                set dl to "false"
                try
                    set dl to ((downloaded of att) as string)
                end try
                set out to out & aname & (character id 31) & amime & (character id 31) & sz & (character id 31) & dl & (character id 30)
            end repeat
        end try
        return out
    end tell
    """
    let raw = try runAppleScript(src)
    var recs = raw.components(separatedBy: RS).filter { !$0.isEmpty }
    guard let head = recs.first else { return ["matched": false, "attachments": []] }
    if head.hasPrefix("MISMATCH") {
        return ["matched": false, "attachments": []]
    }
    recs.removeFirst()
    var out: [[String: Any]] = []
    for rec in recs {
        let f = rec.components(separatedBy: US)
        guard f.count >= 4 else { continue }
        out.append([
            "name": f[0], "mimeType": f[1],
            "size": Int(f[2]) ?? 0, "downloaded": f[3] == "true",
        ])
    }
    return ["matched": true, "attachments": out]
}

func mailGetAt(account: String, mailbox: String, index: Int, expectId: String) throws -> [String: Any] {
    let src = """
    tell application "Mail"
        \(messageRef(account: account, mailbox: mailbox, index: index))
        set actualId to message id of m
        if actualId is not \(asQuote(expectId)) then return "MISMATCH"
        with timeout of 600 seconds
            set b to content of m
        end timeout
        return (subject of m) & (character id 31) & (sender of m) & (character id 31) & b
    end tell
    """
    let raw = try runAppleScript(src)
    if raw == "MISMATCH" { throw SynthError(msg: "index no longer points at \(expectId)") }
    let f = raw.components(separatedBy: US)
    guard f.count >= 3 else { throw SynthError(msg: "unexpected mail_get response") }
    return ["messageId": expectId, "subject": f[0], "sender": f[1],
            "body": f[2...].joined(separator: US)]
}

func mailSaveAttachment(account: String, mailbox: String, index: Int,
                        name: String, directory: String) throws -> [String: Any] {
    try FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
    let dest = (directory as NSString).appendingPathComponent(name)
    let src = """
    tell application "Mail"
        \(messageRef(account: account, mailbox: mailbox, index: index))
        set saved to "no"
        repeat with att in mail attachments of m
            if (name of att) is \(asQuote(name)) then
                with timeout of 600 seconds
                    save att in POSIX file \(asQuote(dest))
                end timeout
                set saved to "yes"
                exit repeat
            end if
        end repeat
        return saved
    end tell
    """
    let ok = try runAppleScript(src)
    guard ok == "yes", FileManager.default.fileExists(atPath: dest) else {
        throw SynthError(msg: "could not save attachment \(name)")
    }
    let attrs = try FileManager.default.attributesOfItem(atPath: dest)
    return ["path": dest, "bytes": (attrs[.size] as? Int) ?? 0, "name": name]
}

/// Forces Mail to fetch bodies for a range of messages by reading their content in bulk.
///
/// Bulk access is the whole point: `content of (messages a thru b of box)` is one Apple
/// Event that makes Mail download every uncached body in the range, where a per-message
/// loop pays a round trip each time. Only the total byte count comes back — the bodies stay
/// in Mail's cache, which is where we want them, and nothing is copied into Synth.
func mailWarm(account: String, mailbox: String, offset: Int, limit: Int) throws -> [String: Any] {
    let src = """
    tell application "Mail"
        set acct to first account whose name is \(asQuote(account))
        -- Gmail nests mailboxes under [Gmail]/, so addressing by leaf name fails with -1728.
        -- Resolve by iteration and accept either the leaf or the full path.
        set box to missing value
        repeat with mb in mailboxes of acct
            set nm to name of mb
            if nm is \(asQuote(mailbox)) or nm ends with ("/" & \(asQuote(mailbox))) then
                set box to mb
                exit repeat
            end if
        end repeat
        if box is missing value then error "no mailbox named " & \(asQuote(mailbox))
        set n to count of messages of box
        set a to \(offset)
        if a > n then return "0" & (character id 31) & (n as string)
        set b to a + \(limit) - 1
        set if_ to 0
        if b > n then set b to n
        set total to 0
        set errMsg to "none"
        -- The default Apple Event timeout is two minutes, which uncached historical
        -- messages routinely exceed while Mail fetches them from the server.
        with timeout of 900 seconds
            try
                set bodies to content of (messages a thru b of box)
                repeat with c in bodies
                    try
                        set total to total + (length of c)
                    end try
                end repeat
            on error e
                set errMsg to e
            end try
        end timeout
        return ((b - a + 1) as string) & (character id 31) & (n as string) & (character id 31) & (total as string) & (character id 31) & errMsg
    end tell
    """
    let raw = try runAppleScript(src)
    let f = raw.components(separatedBy: US)
    return [
        "account": account, "mailbox": mailbox, "offset": offset,
        "fetched": Int(f.first ?? "0") ?? 0,
        "mailboxTotal": f.count > 1 ? (Int(f[1]) ?? 0) : 0,
        "bytes": f.count > 2 ? (Int(f[2]) ?? 0) : 0,
        "error": f.count > 3 ? f[3] : "none",
    ]
}

/// Mailbox inventory: every mailbox in an account with its message count. Used to size a
/// bulk body-download before committing to it.
func mailInventory(account: String) throws -> [[String: Any]] {
    let src = """
    tell application "Mail"
        set acct to first account whose name is \(asQuote(account))
        set out to ""
        repeat with mb in mailboxes of acct
            try
                set out to out & (name of mb) & (character id 31) & ((count of messages of mb) as string) & (character id 30)
            end try
        end repeat
        return out
    end tell
    """
    let raw = try runAppleScript(src)
    var out: [[String: Any]] = []
    for rec in raw.components(separatedBy: RS) where !rec.isEmpty {
        let f = rec.components(separatedBy: US)
        guard f.count >= 2 else { continue }
        out.append(["mailbox": f[0], "count": Int(f[1]) ?? 0])
    }
    return out
}

/// Cheap change sentinel: the id of the newest inbox message plus the message count.
/// Two property fetches per account instead of five bulk lists, so steady-state polling
/// costs ~2s across all accounts. The expensive header fetch runs only when this moves.
func mailProbe(account: String) throws -> [String: Any] {
    let src = """
    tell application "Mail"
        set acct to first account whose name is \(asQuote(account))
        set box to mailbox "INBOX" of acct
        set n to count of messages of box
        if n = 0 then return "0" & (character id 31) & ""
        return (n as string) & (character id 31) & (message id of message 1 of box)
    end tell
    """
    let raw = try runAppleScript(src)
    let f = raw.components(separatedBy: US)
    return [
        "account": account,
        "count": Int(f.first ?? "0") ?? 0,
        "newestId": f.count > 1 ? f[1] : "",
    ]
}

/// Recent inbox headers for one account.
///
/// Bulk property access matters enormously here: asking for `message id of messages 1 thru n`
/// is one Apple Event, where looping `message i of box` is one per message per property. The
/// looping version took over ten minutes across four accounts. This one is seconds.
///
/// Headers only — never `content`, which can force Mail to download the body from the server.
/// Bodies come from mail_get, one explicit message at a time.
func mailRecent(account: String, limit: Int, mailbox: String = "INBOX") throws -> [[String: Any]] {
    let src = """
    tell application "Mail"
        set acct to first account whose name is \(asQuote(account))
        -- Sent mail matters: Synth told Arun to reply to someone he had already replied to,
        -- because it only ever looked at INBOX.
        set box to missing value
        repeat with mb in mailboxes of acct
            set nm to name of mb
            if nm is \(asQuote(mailbox)) or nm ends with ("/" & \(asQuote(mailbox))) then
                set box to mb
                exit repeat
            end if
        end repeat
        if box is missing value then error "no mailbox named " & \(asQuote(mailbox))
        set n to count of messages of box
        if n = 0 then return ""
        if n > \(limit) then set n to \(limit)
        set epochRef to (current date)
        set year of epochRef to 1970
        set month of epochRef to January
        set day of epochRef to 1
        set time of epochRef to 0
        -- Bulk property access must name the reference inline. Assigning the messages to a
        -- variable first materialises a list of specifiers and then each property fetch
        -- fails with -1728.
        -- Index is returned so later calls can address a message directly. Scanning with
        -- `whose message id is ...` walks the whole mailbox and takes minutes on a 23k inbox.
        set ids to message id of (messages 1 thru n of box)
        set subs to subject of (messages 1 thru n of box)
        set sndrs to sender of (messages 1 thru n of box)
        set dts to date received of (messages 1 thru n of box)
        set rds to read status of (messages 1 thru n of box)
        set out to ""
        repeat with i from 1 to n
            set d to ((item i of dts) - epochRef - (time to GMT)) as string
            set out to out & (item i of ids) & (character id 31) & (item i of subs) & (character id 31) & (item i of sndrs) & (character id 31) & d & (character id 31) & ((item i of rds) as string) & (character id 31) & (i as string) & (character id 30)
        end repeat
        return out
    end tell
    """
    let raw = try runAppleScript(src)
    var out: [[String: Any]] = []
    for rec in raw.components(separatedBy: RS) where !rec.isEmpty {
        let f = rec.components(separatedBy: US)
        guard f.count >= 6 else { continue }
        out.append([
            "messageId": f[0], "subject": f[1], "sender": f[2],
            "receivedAt": epochToISO(f[3]), "read": f[4] == "true",
            "index": Int(f[5]) ?? 0,
        ])
    }
    return out
}

func mailGet(messageId: String) throws -> [String: Any] {
    let src = """
    tell application "Mail"
        set found to missing value
        repeat with a in accounts
            try
                set ms to (messages of mailbox "INBOX" of a whose message id is \(asQuote(messageId)))
                if (count of ms) > 0 then
                    set found to item 1 of ms
                    exit repeat
                end if
            end try
        end repeat
        if found is missing value then return ""
    set epochRef to (current date)
        set year of epochRef to 1970
        set month of epochRef to January
        set day of epochRef to 1
        set time of epochRef to 0
        return (subject of found) & (character id 31) & (sender of found) & (character id 31) & (((date received of found) - epochRef - (time to GMT)) as string) & (character id 31) & (content of found)
    end tell
    """
    let raw = try runAppleScript(src)
    if raw.isEmpty { throw SynthError(msg: "no message with id \(messageId) in any inbox") }
    let f = raw.components(separatedBy: US)
    guard f.count >= 4 else { throw SynthError(msg: "unexpected mail_get response") }
    return [
        "messageId": messageId, "subject": f[0], "sender": f[1],
        "receivedAt": epochToISO(f[2]), "body": f[3...].joined(separator: US),
    ]
}

/// Creates a draft. Never sends. `visible:false` keeps a window from stealing focus.
func mailDraft(_ req: [String: Any]) throws -> [String: Any] {
    guard let subject = req["subject"] as? String,
          let body = req["body"] as? String else {
        throw SynthError(msg: "subject and body are required")
    }
    let recipients = (req["to"] as? [String]) ?? []
    guard !recipients.isEmpty else { throw SynthError(msg: "at least one recipient is required") }
    let account = req["account"] as? String
    var senderLine = ""
    if let a = account {
        senderLine = "set sender of msg to (first email address of (first account whose name is \(asQuote(a))))"
    }
    var addLines = ""
    for r in recipients {
        addLines += "\n        make new to recipient at end of to recipients of msg with properties {address:\(asQuote(r))}"
    }
    let src = """
    tell application "Mail"
        set msg to make new outgoing message with properties {subject:\(asQuote(subject)), content:\(asQuote(body)), visible:false}
        \(senderLine)\(addLines)
        save msg
        return "saved"
    end tell
    """
    _ = try runAppleScript(src)
    return ["drafted": true, "subject": subject, "to": recipients, "account": account ?? ""]
}

// MARK: - dispatch

struct SynthError: Error { let msg: String }

func handle(_ req: [String: Any]) -> [String: Any] {
    let cmd = (req["cmd"] as? String) ?? "probe"
    do {
        switch cmd {
        case "status":
            // Reads recorded authorization without requesting, so this never raises a
            // dialog and can be used to check whether a grant actually survived a rebuild.
            return ["ok": true, "result": [
                "calendar": authString(EKEventStore.authorizationStatus(for: .event)),
                "reminders": authString(EKEventStore.authorizationStatus(for: .reminder)),
            ]]
        case "diag":
            return ["ok": true, "result": diag()]
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
            return ["ok": true, "result": listEvents(
                days: days,
                from: parseDate(req["start"] as? String),
                to: parseDate(req["end"] as? String))]
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
        case "delete_reminder":
            return ["ok": true, "result": try deleteReminder(req)]
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
        case "mail_accounts":
            return ["ok": true, "result": try mailAccounts()]
        case "mail_attachments":
            guard let a = req["account"] as? String, let idx = req["index"] as? Int,
                  let mid = req["messageId"] as? String else {
                throw SynthError(msg: "account, index and messageId are required")
            }
            return ["ok": true, "result": try mailAttachments(
                account: a, mailbox: (req["mailbox"] as? String) ?? "INBOX",
                index: idx, expectId: mid)]
        case "mail_source":
            guard let a = req["account"] as? String, let idx = req["index"] as? Int,
                  let mid = req["messageId"] as? String else {
                throw SynthError(msg: "account, index and messageId are required")
            }
            return ["ok": true, "result": try mailSource(
                account: a, mailbox: (req["mailbox"] as? String) ?? "INBOX",
                index: idx, expectId: mid, maxBytes: (req["maxBytes"] as? Int) ?? 400000)]
        case "mail_get_at":
            guard let a = req["account"] as? String, let idx = req["index"] as? Int,
                  let mid = req["messageId"] as? String else {
                throw SynthError(msg: "account, index and messageId are required")
            }
            return ["ok": true, "result": try mailGetAt(
                account: a, mailbox: (req["mailbox"] as? String) ?? "INBOX",
                index: idx, expectId: mid)]
        case "mail_save_attachment":
            guard let a = req["account"] as? String, let idx = req["index"] as? Int,
                  let nm = req["name"] as? String else {
                throw SynthError(msg: "account, index and name are required")
            }
            let dir = (req["directory"] as? String)
                ?? FileManager.default.homeDirectoryForCurrentUser.path + "/Developer/synth/.state/attachments"
            return ["ok": true, "result": try mailSaveAttachment(
                account: a, mailbox: (req["mailbox"] as? String) ?? "INBOX",
                index: idx, name: nm, directory: dir)]
        case "mail_warm":
            guard let a = req["account"] as? String, let mb = req["mailbox"] as? String else {
                throw SynthError(msg: "account and mailbox are required")
            }
            return ["ok": true, "result": try mailWarm(
                account: a, mailbox: mb,
                offset: (req["offset"] as? Int) ?? 1,
                limit: (req["limit"] as? Int) ?? 50)]
        case "mail_inventory":
            guard let a = req["account"] as? String else { throw SynthError(msg: "account is required") }
            return ["ok": true, "result": try mailInventory(account: a)]
        case "mail_probe":
            guard let a = req["account"] as? String else { throw SynthError(msg: "account is required") }
            return ["ok": true, "result": try mailProbe(account: a)]
        case "mail_recent":
            guard let a = req["account"] as? String else { throw SynthError(msg: "account is required") }
            return ["ok": true, "result": try mailRecent(
                account: a, limit: (req["limit"] as? Int) ?? 25,
                mailbox: (req["mailbox"] as? String) ?? "INBOX")]
        case "mail_get":
            guard let m = req["messageId"] as? String else { throw SynthError(msg: "messageId is required") }
            return ["ok": true, "result": try mailGet(messageId: m)]
        case "mail_draft":
            return ["ok": true, "result": try mailDraft(req)]
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

// MARK: - change queue and watchers
//
// Everything that can tell us "something moved" funnels into one JSONL queue. The queue
// records *what* changed and nothing more; deciding whether it matters is the reactor's job,
// which keeps detection free of model calls.

var changeQueuePath = ""
let changeQueueLock = NSLock()

func appendChange(kind: String, detail: String = "") {
    guard !changeQueuePath.isEmpty else { return }
    let line = encode(["at": iso.string(from: Date()), "kind": kind, "detail": detail]) + "\n"
    guard let data = line.data(using: .utf8) else { return }
    changeQueueLock.lock()
    defer { changeQueueLock.unlock() }
    if !FileManager.default.fileExists(atPath: changeQueuePath) {
        FileManager.default.createFile(atPath: changeQueuePath, contents: nil)
    }
    if let fh = FileHandle(forWritingAtPath: changeQueuePath) {
        fh.seekToEndOfFile()
        fh.write(data)
        try? fh.close()
    }
}

func installEventKitObserver() {
    NotificationCenter.default.addObserver(
        forName: .EKEventStoreChanged, object: store, queue: nil
    ) { _ in
        appendChange(kind: "eventkit_changed")
    }
}

/// FSEvents callback is a C function pointer and cannot capture, so the mapping from path
/// prefix to change kind is resolved here from globals.
var watchRoots: [(prefix: String, kind: String)] = []

func kindForPath(_ path: String) -> String? {
    for (prefix, kind) in watchRoots where path.hasPrefix(prefix) {
        return kind
    }
    return nil
}

let fsCallback: FSEventStreamCallback = { _, _, numEvents, eventPaths, _, _ in
    // With kFSEventStreamCreateFlagUseCFTypes this is a CFArray of CFString.
    guard let paths = unsafeBitCast(eventPaths, to: NSArray.self) as? [String] else { return }
    var seen = Set<String>()
    for i in 0..<min(numEvents, paths.count) {
        guard let kind = kindForPath(paths[i]) else { continue }
        if seen.insert(kind).inserted {
            appendChange(kind: kind, detail: paths[i])
        }
    }
}

func installFileWatchers() {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    let candidates: [(String, String)] = [
        ("\(home)/Library/Mail", "mail_changed"),
        ("\(home)/Library/Group Containers/group.com.apple.notes", "notes_changed"),
        ("\(home)/Library/Mobile Documents/com~apple~CloudDocs/Documents", "documents_changed"),
    ]
    var paths: [String] = []
    for (path, kind) in candidates where FileManager.default.fileExists(atPath: path) {
        watchRoots.append((path, kind))
        paths.append(path)
    }
    guard !paths.isEmpty else {
        FileHandle.standardError.write("no watchable paths found\n".data(using: .utf8)!)
        return
    }
    var context = FSEventStreamContext(version: 0, info: nil, retain: nil, release: nil, copyDescription: nil)
    guard let stream = FSEventStreamCreate(
        kCFAllocatorDefault, fsCallback, &context, paths as CFArray,
        FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
        2.0,  // coalesce bursts inside the stream itself
        FSEventStreamCreateFlags(kFSEventStreamCreateFlagNoDefer | kFSEventStreamCreateFlagUseCFTypes)
    ) else {
        FileHandle.standardError.write("FSEventStreamCreate failed\n".data(using: .utf8)!)
        return
    }
    FSEventStreamSetDispatchQueue(stream, DispatchQueue.main)
    FSEventStreamStart(stream)
    FileHandle.standardError.write("watching \(paths.count) paths\n".data(using: .utf8)!)
}

/// Reports whether the daemon can actually reach TCC-protected locations, so a missing
/// Full Disk Access grant surfaces as a diagnostic instead of as mysterious empty results.
func diag() -> [String: Any] {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    var out: [String: Any] = [:]
    for (label, path) in [
        ("mail", "\(home)/Library/Mail"),
        ("notes", "\(home)/Library/Group Containers/group.com.apple.notes"),
        ("documents", "\(home)/Library/Mobile Documents/com~apple~CloudDocs/Documents"),
    ] {
        do {
            let entries = try FileManager.default.contentsOfDirectory(atPath: path)
            out[label] = ["readable": true, "entries": entries.count]
        } catch {
            out[label] = ["readable": false, "error": "\(error.localizedDescription)"]
        }
    }
    out["watching"] = watchRoots.map { $0.kind }
    out["queue"] = changeQueuePath
    return out
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
    changeQueuePath = queuePath
    installEventKitObserver()
    installFileWatchers()

    let q = DispatchQueue(label: "page.akvaithi.synth.accept")
    q.async {
        while true {
            let client = Darwin.accept(fd, nil, nil)
            if client < 0 { continue }

            // Read until EOF. A single read() returns only what is currently buffered, so a
            // request larger than one chunk -- a rendered Notes document, for instance --
            // arrived truncated and the client saw a broken pipe. The client half-closes
            // after writing, which is what ends this loop.
            var request = Data()
            var buf = [UInt8](repeating: 0, count: 65536)
            while true {
                let n = Darwin.read(client, &buf, buf.count)
                if n <= 0 { break }
                request.append(contentsOf: buf[0..<n])
            }

            var response: [String: Any]
            if request.isEmpty {
                response = ["ok": false, "error": "empty request"]
            } else if let obj = try? JSONSerialization.jsonObject(with: request) as? [String: Any] {
                response = handle(obj)
            } else {
                response = ["ok": false, "error": "malformed json request"]
            }

            // Write fully: a large response will not go out in one call either.
            let out = Array((encode(response) + "\n").utf8)
            var written = 0
            while written < out.count {
                let n = out.withUnsafeBufferPointer { p -> Int in
                    Darwin.write(client, p.baseAddress! + written, out.count - written)
                }
                if n <= 0 { break }
                written += n
            }
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

// rebuild marker 1787266493
