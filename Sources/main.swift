import Cocoa
import UserNotifications

// 菜单栏常驻：一眼看到今天积分领没领到、哪笔快过期、按现在的速度还能用几天。
// 真正干活的还是同目录下的 领积分.py（定时由 launchd 跑），这里只读它写的两个文件。

/// 数据目录不再写死：环境变量优先，其次读打包时写进 bundle 的 project.path，最后退到 ~/续杯
func resolveProject() -> String {
    if let p = ProcessInfo.processInfo.environment["XUPEI_PROJECT"], !p.isEmpty { return p }
    if let res = Bundle.main.resourcePath,
       let txt = try? String(contentsOfFile: res + "/project.path", encoding: .utf8) {
        let t = txt.trimmingCharacters(in: .whitespacesAndNewlines)
        if !t.isEmpty { return t }
    }
    return ("~/续杯" as NSString).expandingTildeInPath
}

let PROJECT = resolveProject()
let STATUS_JSON = PROJECT + "/今日状态.json"
let LEDGER = PROJECT + "/领积分台账.jsonl"
let LOG_FILE = PROJECT + "/领积分.log"
let PYTHON = PROJECT + "/.venv/bin/python"
let SCRIPT = PROJECT + "/领积分.py"
let REVEAL = PROJECT + "/.唤出图标"
let REVEAL_WINDOW: TimeInterval = 3600
let INVENTORY = PROJECT + "/积分盘查.json"
let NOTIFIED_KEY = "campaignNotified"

struct Item: Codable {
    let name: String
    let state: String
    let detail: String
}

struct Status: Codable {
    let date: String
    let ran_at: String
    let items: [Item]
}

struct AddOn: Codable {
    var total: Int?
    var used: Int?
    var remaining: Int?
}

struct LedgerLine: Codable {
    var type: String
    var service: String?
    var date: String?
    var amount: Int?
    var expires_at: String?
    var add_on: AddOn?
}

struct InvApp: Codable {
    var key: String
    var name: String
    var app: String?
    var unit: String?
    var total: Double?
    var remaining: Double?
    var purchased: Double?
    var gifted: Double?
    var used: Double?
    var quota_known: Bool?
    var expires: String?
    var note: String?
}

struct Campaign: Codable {
    var source: String
    var text: String
    var url: String?
    var found_at: String
}

struct Inventory: Codable {
    var updated_at: String
    var claimed_since: String?
    var apps: [InvApp]
    var campaigns: [Campaign]?
}

let dayFormat: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd"
    return f
}()

let stampFormat: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssZ"
    return f
}()

let minuteFormat: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm"
    return f
}()

func freshCampaigns(_ list: [Campaign], now: Date) -> [Campaign] {
    list.filter { c in
        guard let t = minuteFormat.date(from: c.found_at) else { return false }
        return now.timeIntervalSince(t) < 86400
    }
}

func campaignKey(_ c: Campaign) -> String { "\(c.source)|\(c.text)" }

/// 新活动发一条系统通知（由本 App 发，图标才是自己的礼物卡，不是脚本默认图标）。
/// 同一条只提醒一次；升级后第一次只记基线，不把历史条目当成新活动弹出来。
func notifyNewCampaigns(_ fresh: [Campaign]) {
    let defaults = UserDefaults.standard
    if defaults.object(forKey: NOTIFIED_KEY) == nil {
        defaults.set(fresh.map(campaignKey), forKey: NOTIFIED_KEY)
        return
    }
    var seen = defaults.stringArray(forKey: NOTIFIED_KEY) ?? []
    let news = fresh.filter { !seen.contains(campaignKey($0)) }
    guard !news.isEmpty else { return }
    seen.append(contentsOf: news.map(campaignKey))
    if seen.count > 400 { seen.removeFirst(seen.count - 400) }
    defaults.set(seen, forKey: NOTIFIED_KEY)

    let content = UNMutableNotificationContent()
    content.title = news.count == 1 ? "新积分活动：\(news[0].source)" : "新积分活动 \(news.count) 条"
    content.body = news.prefix(3).map { String($0.text.prefix(60)) }.joined(separator: "；")
    let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
    UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
}


func parseStamp(_ text: String) -> Date? {
    // 服务端给的是 2026-11-15T02:04:26.576304Z，截到秒再解析
    let head = String(text.prefix(19)) + "Z"
    return stampFormat.date(from: head)
}

func loadStatus() -> Status? {
    guard let data = FileManager.default.contents(atPath: STATUS_JSON) else { return nil }
    return try? JSONDecoder().decode(Status.self, from: data)
}

func loadInventory() -> Inventory? {
    guard let data = FileManager.default.contents(atPath: INVENTORY),
          let inv = try? JSONDecoder().decode(Inventory.self, from: data) else { return nil }
    return inv
}

func appIcon(_ path: String, size: CGFloat = 18) -> NSImage? {
    guard FileManager.default.fileExists(atPath: path) else { return nil }
    let source = NSWorkspace.shared.icon(forFile: path)
    let points = NSSize(width: size, height: size)
    // 必须画成真实位图：延迟绘制的 NSImage 菜单项可能拿不到像素，图标就不显示
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size * 2),
                                     pixelsHigh: Int(size * 2), bitsPerSample: 8,
                                     samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                     colorSpaceName: .deviceRGB, bytesPerRow: 0,
                                     bitsPerPixel: 0) else { return nil }
    rep.size = points
    NSGraphicsContext.saveGraphicsState()
    if let ctx = NSGraphicsContext(bitmapImageRep: rep) {
        NSGraphicsContext.current = ctx
        ctx.imageInterpolation = .high
        // 各家图标自带约 7% 透明留白；放大 16% 裁掉，让可见图形统一填满同一尺寸
        let scale: CGFloat = 1.16
        let draw = NSRect(x: points.width * (1 - scale) / 2,
                          y: points.height * (1 - scale) / 2,
                          width: points.width * scale, height: points.height * scale)
        NSBezierPath(rect: NSRect(origin: .zero, size: points)).addClip()
        source.draw(in: draw)
    }
    NSGraphicsContext.restoreGraphicsState()
    let image = NSImage(size: points)
    image.addRepresentation(rep)
    return image
}

func loadLedger() -> [LedgerLine] {
    guard let text = try? String(contentsOfFile: LEDGER, encoding: .utf8) else { return [] }
    return text.split(separator: "\n").compactMap {
        try? JSONDecoder().decode(LedgerLine.self, from: Data($0.utf8))
    }
}

func stateWord(_ state: String) -> String {
    switch state {
    case "claimed": return "刚领到"
    case "already": return "已领过"
    case "none": return "没可领"
    default: return "失败"
    }
}

/// 菜单栏标题、主菜单一句话结论、子菜单里的明细。App 和 --dump 都走这里。
func compose(snapshot: Status?, running: Bool, ledger: [LedgerLine],
             campaigns: [Campaign]) -> (String, String, String, [String]) {
    var title = " 没记录"
    var lines = ["", "", ""]
    let now = Date()
    let today = dayFormat.string(from: now)

    // 第一行：今天跑没跑、各家结果
    if let s = snapshot {
        let done = s.items.filter { $0.state == "claimed" || $0.state == "already" }.count
        let bad = s.items.filter { $0.state == "error" || $0.state == "none" }
        title = running ? " 正在领…"
            : (bad.isEmpty ? " \(done)/\(s.items.count)" : " \(done)/\(s.items.count) 有异常")
        let verdict = s.items.map { "\($0.name) \(stateWord($0.state))" }.joined(separator: "、")
        lines[0] = (s.date == today ? "今天 \(s.ran_at) 跑过：" : "最近一次 \(s.date) \(s.ran_at)：")
            + verdict
            + (bad.isEmpty ? "" : "｜" + bad.map { "\($0.name)：\($0.detail)" }.joined(separator: "；"))
    } else {
        lines[0] = "还没跑过一次领积分"
    }

    // 第二行：按软件分别列最早到期
    let grants = ledger.filter { $0.type == "grant" }
        .compactMap { g -> (String, Date, Int)? in
            guard let text = g.expires_at, let when = parseStamp(text), when > now else { return nil }
            return (g.service ?? "?", when, g.amount ?? 0)
        }
    if grants.isEmpty {
        lines[1] = "到期：台账里还没有未过期的领取记录"
    } else {
        let df = DateFormatter()
        df.dateFormat = "M月d日"
        var groups: [String: [(Date, Int)]] = [:]
        for (svc, when, amount) in grants { groups[svc, default: []].append((when, amount)) }
        let parts = groups.map { svc, items -> (Date, String) in
            let soon = items.map { $0.0 }.min()!
            let days = Calendar.current.dateComponents([.day], from: now, to: soon).day ?? 0
            let total = items.reduce(0) { $0 + $1.1 }
            return (soon, "\(svc) \(df.string(from: soon))（\(days) 天后到期，\(items.count) 笔 \(total) 分）")
        }.sorted { $0.0 < $1.0 }.map { $0.1 }
        lines[1] = "到期：" + parts.joined(separator: "；")
    }

    // 第三行：消耗速度。用 Qoder 每天最后一条余额快照算日均，再看余额够用几天
    var byDate: [String: AddOn] = [:]
    for row in ledger where row.type == "balance" && row.service == "Qoder" && row.add_on != nil {
        if let d = row.date { byDate[d] = row.add_on }   // 后一条覆盖前一条 = 取当天最后一次
    }
    let dates = byDate.keys.sorted()
    if let lastKey = dates.last, let last = byDate[lastKey] {
        var label = "今天"
        if lastKey != today {
            let parts = lastKey.split(separator: "-")
            if parts.count == 3, let m = Int(parts[1]), let d = Int(parts[2]) {
                label = "最近一次（\(m)月\(d)日）"
            } else {
                label = "最近一次（\(lastKey)）"
            }
        }
        var text = "Qoder 消耗：\(label)已用 \(last.used ?? 0) 分，剩 \(last.remaining ?? 0) 分"
        if dates.count >= 2, let firstKey = dates.first, let first = byDate[firstKey],
           let a = dayFormat.date(from: firstKey), let b = dayFormat.date(from: lastKey) {
            let span = max(1, Calendar.current.dateComponents([.day], from: a, to: b).day ?? 1)
            let burned = Double((last.used ?? 0) - (first.used ?? 0)) / Double(span)
            if burned > 0 {
                text = String(format: "Qoder 消耗：近 %d 天日均 %.0f 分，剩 %d 分大约够用 %d 天",
                              span, burned, last.remaining ?? 0,
                              Int(Double(last.remaining ?? 0) / burned))
            }
        }
        lines[2] = text
    } else {
        lines[2] = "Qoder 消耗：还没有用量快照"
    }

    let fresh = freshCampaigns(campaigns, now: now)
    if !fresh.isEmpty {
        title += " ★"
        lines[0] += "｜新活动 \(fresh.count) 条"
    }

    // 菜单头两行：结论 + 补充
    var head: String
    var sub: String
    if running {
        head = "正在领取…"
        sub = ""
    } else if let s = snapshot {
        let bad = s.items.filter { $0.state == "error" || $0.state == "none" }
        if s.date != today {
            head = "今天还没跑"
            sub = "最近一次 \(s.date) \(s.ran_at)"
        } else if !bad.isEmpty {
            head = "今天有异常"
            sub = bad.map { "\($0.name)：\($0.detail)" }.joined(separator: "；")
        } else {
            head = "今天 \(s.ran_at) 已领齐"
            var bits: [String] = []
            if let last = ledger.last(where: { $0.type == "balance" && $0.service == "Qoder" && $0.add_on != nil }),
               let remaining = last.add_on?.remaining {
                bits.append("Qoder 剩 \(remaining)")
            }
            if let first = grants.min(by: { $0.1 < $1.1 }) {
                let df = DateFormatter()
                df.dateFormat = "M月d日"
                let day = df.string(from: first.1)
                // 前一段已经写了 Qoder，同一家的到期就别再把名字重复一遍
                bits.append(first.0 == "Qoder" ? "最早到期 \(day)" : "\(day) 最早到期（\(first.0)）")
            }
            sub = bits.joined(separator: " · ")
        }
    } else {
        head = "还没有记录"
        sub = "点一次「立即领一次」"
    }
    if !fresh.isEmpty {
        sub += (sub.isEmpty ? "" : " · ") + "新活动 \(fresh.count) 条"
    }
    return (title, head, sub, lines)
}

/// 只有在"今天该领的没领到"时才占菜单栏，其余时间不显示，省位子。
func shouldShow(_ snapshot: Status?, now: Date) -> Bool {
    guard let s = snapshot else { return false }
    if s.date != dayFormat.string(from: now) {
        // 结论不是今天的：过了早上 10 点（新一轮已开）还没拿到今天的结论，就算异常
        return Calendar.current.component(.hour, from: now) >= 10
    }
    return s.items.contains { $0.state == "error" || $0.state == "none" }
}

/// 藏起来之后使用者还能双击 /Applications 里的 App 把它叫出来一会儿，不然连"立即领一次"都点不到
func requestReveal() {
    try? Data("\(Int(Date().timeIntervalSince1970))".utf8).write(to: URL(fileURLWithPath: REVEAL))
}

func revealRequested(now: Date) -> Bool {
    guard let text = try? String(contentsOfFile: REVEAL, encoding: .utf8),
          let stamp = TimeInterval(text.trimmingCharacters(in: .whitespacesAndNewlines)) else { return false }
    return now.timeIntervalSince1970 - stamp < REVEAL_WINDOW
}

func menuBarVisible(snapshot: Status?, now: Date, hasCampaign: Bool) -> Bool {
    shouldShow(snapshot, now: now) || revealRequested(now: now) || hasCampaign
}

func fmtAmount(_ n: Double) -> String {
    if n >= 100_000_000 { return String(format: "%.0f 亿", n / 100_000_000) }
    if n >= 10_000 { return String(format: "%.0f 万", n / 10_000) }
    return String(format: "%.0f", n)
}

/// 菜单栅格：自定义行要和系统菜单项（图标列≈18、标题列≈42）共用一套竖线，否则混排会错位
let GRID_ICON_X: CGFloat = 18
let GRID_TEXT_X: CGFloat = 42
let GRID_RIGHT: CGFloat = 16
let MENU_MIN_W: CGFloat = 340
let MENU_MAX_W: CGFloat = 470

/// 菜单里最近三条活动，一行一句
func campaignLines(_ campaigns: [Campaign]) -> [String] {
    campaigns.suffix(3).reversed().map { c in
        let text = c.text.count > 40 ? String(c.text.prefix(40)) + "…" : c.text
        return "\(c.source)：\(text)"
    }
}

/// 菜单宽度由最宽的一项决定。纯文本行（到期/消耗/动作）比自定义行长，行宽必须跟着它走，
/// 否则右边距空出一截，六条进度条看着短一截。78 = 系统左缩进 42 + 右侧内边距与滚动条余量。
func menuRowWidth(detailLines: [String], apps: [InvApp], campaigns: [Campaign]) -> CGFloat {
    var texts = Array(detailLines.dropFirst()) + campaignLines(campaigns)
    let unreadable = apps.filter { $0.total == nil }
    if !unreadable.isEmpty {
        texts.append("读不到余额：" + unreadable.map { $0.name }.joined(separator: "、"))
    }
    texts += ["立即领一次", "看今天的记录", "关于 续杯", "退出"]
    let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.menuFont(ofSize: 0)]
    let widest = texts
        .map { (($0 as NSString).size(withAttributes: attrs).width).rounded(.up) + 78 }
        .max() ?? MENU_MIN_W
    return min(max(widest, MENU_MIN_W), MENU_MAX_W)
}

/// 菜单头：粗体结论 + 一行补充
final class HeaderRow: NSView {
    init(head: String, sub: String, width: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 44))
        let textW = width - GRID_TEXT_X - GRID_RIGHT
        let title = NSTextField(labelWithString: head)
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        title.textColor = .labelColor
        title.lineBreakMode = .byTruncatingTail
        title.frame = NSRect(x: GRID_TEXT_X, y: 23, width: textW, height: 18)
        addSubview(title)
        if !sub.isEmpty {
            let detail = NSTextField(labelWithString: sub)
            detail.font = .systemFont(ofSize: 11)
            detail.textColor = .secondaryLabelColor
            detail.lineBreakMode = .byTruncatingTail
            detail.frame = NSRect(x: GRID_TEXT_X, y: 6, width: textW, height: 15)
            addSubview(detail)
        }
    }
    required init?(coder: NSCoder) { fatalError() }
}

/// 图例：三个渐变圆点 + 文字
final class LegendRow: NSView {
    private let items: [(String, NSColor, NSColor)] = [
        ("买的", .systemBlue, .systemCyan),
        ("送的", .systemGreen, .systemMint),
        ("已用", .tertiaryLabelColor, .quaternaryLabelColor),
    ]

    init(width: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 18))
    }

    required init?(coder: NSCoder) { fatalError() }

    override func draw(_ dirtyRect: NSRect) {
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 11),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]
        var x: CGFloat = GRID_TEXT_X
        for (text, start, end) in items {
            let dot = NSRect(x: x, y: (bounds.height - 8) / 2, width: 8, height: 8)
            let path = NSBezierPath(ovalIn: dot)
            if let gradient = NSGradient(starting: start, ending: end) {
                gradient.draw(in: path, angle: -60)
            } else {
                start.setFill()
                path.fill()
            }
            x += 8 + 5
            let size = (text as NSString).size(withAttributes: attrs)
            (text as NSString).draw(at: NSPoint(x: x, y: (bounds.height - size.height) / 2),
                                    withAttributes: attrs)
            x += size.width + 14
        }
    }
}

/// 渐变分段进度条
final class ProgressBarView: NSView {
    private let segments: [(Double, NSColor, NSColor)]

    init(frame: NSRect, segments: [(Double, NSColor, NSColor)]) {
        self.segments = segments
        super.init(frame: frame)
    }

    required init?(coder: NSCoder) { fatalError() }

    override func draw(_ dirtyRect: NSRect) {
        let clip = NSBezierPath(roundedRect: bounds,
                                xRadius: bounds.height / 2, yRadius: bounds.height / 2)
        NSColor.separatorColor.withAlphaComponent(0.45).setFill()
        clip.fill()
        NSGraphicsContext.saveGraphicsState()
        clip.addClip()
        var x: CGFloat = 0
        for (fraction, start, end) in segments where fraction > 0 {
            let w = bounds.width * CGFloat(fraction)
            let rect = NSRect(x: x, y: 0, width: max(w - 1.5, 0.5), height: bounds.height)
            if let gradient = NSGradient(starting: start, ending: end) {
                gradient.draw(in: rect, angle: 0)
            } else {
                start.setFill()
                rect.fill()
            }
            x += w
        }
        NSGraphicsContext.restoreGraphicsState()
    }
}

/// 一家软件一行：图标 + 名称 + 剩余/总量 + 渐变细进度条（蓝=买的 绿=送的 灰=已用）
final class AppBarRow: NSView {
    init(app: InvApp, width: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 46))
        let barW = width - GRID_TEXT_X - GRID_RIGHT
        if let path = app.app, let icon = appIcon(path) {
            let iv = NSImageView(frame: NSRect(x: GRID_ICON_X, y: 25, width: 18, height: 18))
            iv.image = icon
            addSubview(iv)
        }
        let label = NSTextField(labelWithString: app.name)
        label.font = .systemFont(ofSize: 13, weight: .medium)
        label.textColor = .labelColor
        label.lineBreakMode = .byTruncatingTail
        label.frame = NSRect(x: GRID_TEXT_X, y: 25, width: 148, height: 18)
        addSubview(label)

        let value = NSTextField(labelWithAttributedString: Self.valueText(app))
        value.alignment = .right
        value.frame = NSRect(x: GRID_TEXT_X + 148, y: 25,
                             width: barW - 148, height: 18)
        addSubview(value)

        if let expires = app.expires, !expires.isEmpty {
            let when = NSTextField(labelWithString: expires)
            when.font = .systemFont(ofSize: 10.5)
            when.textColor = .secondaryLabelColor
            when.lineBreakMode = .byTruncatingTail
            when.frame = NSRect(x: GRID_TEXT_X, y: 11, width: barW, height: 12)
            addSubview(when)
        }

        var fractions: [(Double, NSColor, NSColor)] = []
        if app.quota_known == false {
            // 没有周期总量：有余额就画满格，余额为 0 就空着，别画成满格
            if (app.remaining ?? 0) > 0 {
                fractions = [(1.0, .systemGreen, .systemMint)]
            }
        } else if let total = app.total, total > 0 {
            let remaining = min(app.remaining ?? 0, total)
            let purchasedRemaining = min(app.purchased ?? 0, remaining)
            let giftedRemaining = max(remaining - purchasedRemaining, 0)
            let used = max(total - remaining, 0)
            fractions = [
                (purchasedRemaining / total, .systemBlue, .systemCyan),
                (giftedRemaining / total, .systemGreen, .systemMint),
                (used / total, .tertiaryLabelColor, .quaternaryLabelColor),
            ]
        }
        let bar = ProgressBarView(frame: NSRect(x: GRID_TEXT_X, y: 4, width: barW, height: 5),
                                  segments: fractions)
        addSubview(bar)
    }

    required init?(coder: NSCoder) { fatalError() }

    /// 数字用等宽中黑、前后缀用次要灰，六行的数值才会在同一列上立得住
    static func valueText(_ app: InvApp) -> NSAttributedString {
        let unit = app.unit ?? ""
        let out = NSMutableAttributedString()
        let dim: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 11.5),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]
        let strong: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 11.5, weight: .medium),
            .foregroundColor: NSColor.labelColor,
        ]
        func add(_ s: String, _ a: [NSAttributedString.Key: Any]) {
            out.append(NSAttributedString(string: s, attributes: a))
        }
        guard let remaining = app.remaining else {
            if unit == "%" { add("读不到", dim) }
            return out
        }
        add("剩 ", dim)
        if unit == "%" {
            add(String(format: "%.0f", remaining), strong)
            add("%", dim)
        } else if app.quota_known == false {
            add(fmtAmount(remaining), strong)
            add(" \(unit)", dim)
        } else if let total = app.total {
            add("\(fmtAmount(remaining)) / \(fmtAmount(total))", strong)
            add(" \(unit)", dim)
        }
        // 属性字符串自带的段落样式会盖掉 NSTextField.alignment，右对齐必须写在这里
        let para = NSMutableParagraphStyle()
        para.alignment = .right
        out.addAttribute(.paragraphStyle, value: para,
                         range: NSRange(location: 0, length: out.length))
        return out
    }
}

/// 前台 App 的 bundle id → 盘查里的 key 和图标路径
let SUPPORTED: [String: (key: String, path: String)] = [
    "com.qodercn.app": ("qoder", "/Applications/Qoder CN.app"),
    "cn.trae.app": ("trae", "/Applications/Trae CN.app"),
    "com.minimax.agent.cn": ("minimax", "/Applications/MiniMax Code.app"),
    "com.moonshot.kimichat": ("kimi", "/Applications/Kimi.app"),
    "dev.zcode.app": ("zcode", "/Applications/ZCode.app"),
    "com.deepseek.dsh": ("deepseek", "/Applications/DeepSeek Harness.app"),
]

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var status: NSStatusItem?
    var menuOpen = false
    var running = false
    var snapshot: Status?
    var timer: Timer?
    var headText = ""
    var subText = ""
    var detailLines: [String] = []
    var activeKey: String?
    var activePath: String?
    var lastBalance: [String: Date] = [:]
    var balanceProcess: Process?

    func applicationDidFinishLaunching(_ note: Notification) {
        // 只留一份：已经有一个在跑，就把它的图标叫出来，然后这份退出
        let mine = NSRunningApplication.current
        let twins = NSRunningApplication.runningApplications(withBundleIdentifier: mine.bundleIdentifier ?? "")
            .filter { $0.processIdentifier != mine.processIdentifier }
        if !twins.isEmpty {
            requestReveal()
            NSApp.terminate(nil)
            return
        }

        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(appActivated(_:)),
            name: NSWorkspace.didActivateApplicationNotification, object: nil)
        if let front = NSWorkspace.shared.frontmostApplication {
            handleActive(bundle: front.bundleIdentifier, path: front.bundleURL?.path)
        }
        // 只要横幅权限；用户没点允许就一条都不发
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { _, _ in }
        // 刚启动时 frontmostApplication 可能还是自己，多等两次再认
        for delay in [1.5, 5.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                if let front = NSWorkspace.shared.frontmostApplication {
                    self?.handleActive(bundle: front.bundleIdentifier, path: front.bundleURL?.path)
                }
                self?.refresh()
            }
        }

        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            guard let self else { return }
            if self.activeKey == nil, let front = NSWorkspace.shared.frontmostApplication {
                self.handleActive(bundle: front.bundleIdentifier, path: front.bundleURL?.path)
            }
            self.refresh()
        }
    }

    /// 使用者切到哪个受支持的 App，就把菜单栏换成它的剩余百分比
    @objc func appActivated(_ note: Notification) {
        let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
        handleActive(bundle: app?.bundleIdentifier, path: app?.bundleURL?.path)
        refresh()
    }

    func handleActive(bundle: String?, path: String?) {
        guard let bundle, let info = SUPPORTED[bundle] else {
            activeKey = nil
            activePath = nil
            return
        }
        if activeKey != info.key {
            activeKey = info.key
            activePath = info.path
            refreshBalance(force: true)
        }
    }

    /// 只刷新当前前台这家的余额；Qoder 要起 QoderWake（约 8 秒），所以间隔放长
    func refreshBalance(force: Bool = false) {
        guard let key = activeKey, balanceProcess == nil else { return }
        let interval: TimeInterval = key == "qoder" ? 180 : 45
        if !force, let last = lastBalance[key], Date().timeIntervalSince(last) < interval { return }
        lastBalance[key] = Date()
        let task = Process()
        task.executableURL = URL(fileURLWithPath: PYTHON)
        task.arguments = [SCRIPT, "--balance", key]
        task.currentDirectoryURL = URL(fileURLWithPath: PROJECT)
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        task.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.balanceProcess = nil
                self?.refresh()
            }
        }
        do {
            try task.run()
            balanceProcess = task
        } catch {
            balanceProcess = nil
        }
    }

    func percent(_ app: InvApp) -> Int? {
        guard app.quota_known != false else { return nil }
        if app.unit == "%" { return Int((app.remaining ?? 0).rounded()) }
        guard let total = app.total, total > 0, let remaining = app.remaining else { return nil }
        return Int((remaining / total * 100).rounded())
    }

    func setStatusIcon(_ path: String?) {
        if let path, let icon = appIcon(path) {
            status?.button?.image = icon
        } else if let image = NSImage(systemSymbolName: "mug.fill", accessibilityDescription: "续杯") {
            image.isTemplate = true
            status?.button?.image = image
        }
    }

    func buildStatusItem() -> NSStatusItem {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let image = NSImage(systemSymbolName: "mug.fill", accessibilityDescription: "续杯") {
            image.isTemplate = true
            item.button?.image = image
        }
        return item
    }

    /// 双击 /Applications 里的 App（或 `open -a`）时，让藏起来的图标重新露出来
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        requestReveal()
        refresh()
        return true
    }

    func menuWillOpen(_ menu: NSMenu) {
        menuOpen = true
    }

    func menuDidClose(_ menu: NSMenu) {
        menuOpen = false
        DispatchQueue.main.async { [weak self] in self?.rebuildMenu() }
    }

    func line(_ text: String) -> NSMenuItem {
        let item = NSMenuItem(title: text, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    func action(_ text: String, _ sel: Selector, symbol: String? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: text, action: sel, keyEquivalent: "")
        item.target = self
        if let symbol, let base = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) {
            let icon = base.withSymbolConfiguration(
                NSImage.SymbolConfiguration(pointSize: 12, weight: .regular)) ?? base
            icon.isTemplate = true
            item.image = icon
            if #available(macOS 27.0, *) { item.preferredImageVisibility = .visible }
        }
        return item
    }

    func refresh() {
        snapshot = loadStatus()
        let inv = loadInventory()
        let campaigns = inv?.campaigns ?? []
        let (title, head, sub, details) = compose(snapshot: snapshot, running: running,
                                                  ledger: loadLedger(), campaigns: campaigns)
        headText = head
        subText = sub
        detailLines = details
        let freshList = freshCampaigns(campaigns, now: Date())
        notifyNewCampaigns(freshList)
        let hasFresh = !freshList.isEmpty
        if menuBarVisible(snapshot: snapshot, now: Date(), hasCampaign: hasFresh) || activeKey != nil {
            if status == nil { status = buildStatusItem() }
            if let key = activeKey,
               let app = inv?.apps.first(where: { $0.key == key }) {
                if let pct = percent(app) {
                    status?.button?.title = " \(pct)%"
                } else if let remaining = app.remaining {
                    status?.button?.title = " \(fmtAmount(remaining)) \(app.unit ?? "")"
                } else {
                    status?.button?.title = " —"
                }
                setStatusIcon(activePath ?? app.app)
            } else if activeKey != nil {
                status?.button?.title = " —"
                setStatusIcon(activePath)
            } else {
                status?.button?.title = title
                setStatusIcon(nil)
            }
            if !menuOpen { rebuildMenu() }
            refreshBalance()
        } else if let item = status {
            NSStatusBar.system.removeStatusItem(item)
            status = nil
        }
    }

    /// 主菜单：一句话结论 + 每家一条彩色进度条 + 到期/消耗 + 动作
    func rebuildMenu() {
        guard let status else { return }
        let menu = NSMenu()
        menu.delegate = self
        menu.autoenablesItems = false
        let inv = loadInventory()
        let apps = inv?.apps ?? []
        let campaigns = inv?.campaigns ?? []
        let unreadable = apps.filter { $0.total == nil }
        let cLines = campaignLines(campaigns)
        let rowW = menuRowWidth(detailLines: detailLines, apps: apps, campaigns: campaigns)

        let header = NSMenuItem()
        header.view = HeaderRow(head: headText, sub: subText, width: rowW)
        menu.addItem(header)
        menu.addItem(.separator())

        for a in apps where a.total != nil {
            let item = NSMenuItem()
            item.view = AppBarRow(app: a, width: rowW)
            item.toolTip = a.note
            menu.addItem(item)
        }
        if !unreadable.isEmpty {
            let item = line("读不到余额：" + unreadable.map { $0.name }.joined(separator: "、"))
            item.toolTip = unreadable.compactMap { a in a.note.map { "\(a.name)：\($0)" } }
                .joined(separator: "\n")
            menu.addItem(item)
        }
        if apps.contains(where: { $0.total != nil }) {
            let legend = NSMenuItem()
            legend.view = LegendRow(width: rowW)
            menu.addItem(legend)
        }
        if detailLines.count > 1 {
            menu.addItem(.separator())
            for text in detailLines.dropFirst() { menu.addItem(line(text)) }
        }
        if !cLines.isEmpty {
            menu.addItem(.separator())
            for text in cLines { menu.addItem(line(text)) }
        }
        menu.addItem(.separator())
        menu.addItem(action("立即领一次", #selector(runOnce), symbol: "arrow.clockwise"))
        menu.addItem(action("看今天的记录", #selector(openLog), symbol: "doc.text"))
        menu.addItem(.separator())
        menu.addItem(action("关于 续杯", #selector(about), symbol: "info.circle"))
        menu.addItem(action("退出", #selector(quit), symbol: "power"))
        status.menu = menu
    }

    @objc func runOnce() {
        guard !running else { return }
        running = true
        refresh()
        let task = Process()
        task.executableURL = URL(fileURLWithPath: PYTHON)
        task.arguments = [SCRIPT]
        task.currentDirectoryURL = URL(fileURLWithPath: PROJECT)
        task.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.running = false
                self.refresh()
                let text = self.snapshot.map { s in
                    "今天 \(s.ran_at)：" + s.items.map { "\($0.name) \($0.detail)" }.joined(separator: "；")
                } ?? "跑完了，但状态文件没写出来"
                let alert = NSAlert()
                alert.messageText = "领积分"
                alert.informativeText = text
                alert.runModal()
            }
        }
        do {
            try task.run()
        } catch {
            running = false
            refresh()
            let alert = NSAlert()
            alert.messageText = "没能启动领积分"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }

    @objc func openLog() {
        // 在访达里选中日志文件，不去猜默认 App（使用者要求：别打开浏览器）
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: LOG_FILE)])
    }

    @objc func about() {
        let credits = NSMutableAttributedString()
        credits.append(NSAttributedString(
            string: "每天自动领各家 AI 编程工具的免费额度，菜单栏盯住剩余额度和到期日。\n\n",
            attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.labelColor]))
        credits.append(NSAttributedString(
            string: "开发者：Eric Liu @ zhengcheng_mac@icloud.com",
            attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor]))
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "续杯",
            .applicationVersion: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "—",
            .credits: credits,
        ])
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }
}

if CommandLine.arguments.contains("--render-rows") {
    _ = NSApplication.shared
    func save(_ view: NSView, _ name: String) {
        let box = NSView(frame: view.frame)
        box.wantsLayer = true
        box.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        box.addSubview(view)
        box.layoutSubtreeIfNeeded()
        guard let rep = box.bitmapImageRepForCachingDisplay(in: box.bounds) else { return }
        box.cacheDisplay(in: box.bounds, to: rep)
        if let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: "/tmp/row-\(name).png"))
        }
    }
    let inv = loadInventory()
    let apps = inv?.apps ?? []
    let campaigns = inv?.campaigns ?? []
    let (_, head, sub, lines) = compose(snapshot: loadStatus(), running: false,
                                        ledger: loadLedger(), campaigns: campaigns)
    // 和 rebuildMenu 用同一个算宽函数，渲染出来的才是菜单里真实的宽度
    let w = menuRowWidth(detailLines: lines, apps: apps, campaigns: campaigns)
    save(HeaderRow(head: head, sub: sub, width: w), "header")
    save(LegendRow(width: w), "legend")
    for a in apps where a.total != nil {
        save(AppBarRow(app: a, width: w), a.key)
    }
    exit(0)
}

// --dump：不开界面，把"该不该显示 + 显示什么"原样打出来，方便验证
if CommandLine.arguments.contains("--dump") {
    let snapshot = loadStatus()
    let now = Date()
    let inv = loadInventory()
    let campaigns = inv?.campaigns ?? []
    let (title, head, sub, lines) = compose(snapshot: snapshot, running: false,
                                            ledger: loadLedger(), campaigns: campaigns)
    let problem = shouldShow(snapshot, now: now)
    let reveal = revealRequested(now: now)
    let fresh = freshCampaigns(campaigns, now: now)
    // 和 refresh() 用同一套判据：正在用某家软件时，菜单栏会换成它的实时余额
    _ = NSApplication.shared
    let front = NSWorkspace.shared.frontmostApplication
    var activeKey: String?
    if let id = front?.bundleIdentifier { activeKey = SUPPORTED[id]?.key }
    var reason = problem ? "今天没领到或有失败"
        : (reveal ? "你刚唤出过，还在 1 小时里"
                  : (fresh.isEmpty ? "今天正常，没必要占位" : "有 \(fresh.count) 条新活动"))
    if let key = activeKey { reason += "；正在用 \(key)，显示它的实时余额" }
    let visible = menuBarVisible(snapshot: snapshot, now: now, hasCampaign: !fresh.isEmpty)
        || activeKey != nil
    let frontName = front?.localizedName ?? "认不到"
    print("菜单栏：\(visible ? "显示" : "隐藏")（\(reason)｜当前前台：\(frontName)）")
    print("标题：\(title)")
    print("结论：\(head)")
    if !sub.isEmpty { print("补充：\(sub)") }
    for (i, text) in lines.enumerated() { print("第\(i + 1)行：\(text)") }
    if let inv = inv {
        print("积分盘查（更新于 \(inv.updated_at)）：")
        for a in inv.apps {
            let when = (a.expires?.isEmpty == false) ? "  有效期: \(a.expires!)" : ""
            if let total = a.total, let remaining = a.remaining {
                print("  \(a.name)  剩 \(fmtAmount(remaining)) / \(fmtAmount(total)) \(a.unit ?? "")"
                      + "  [\(a.note ?? "")]\(when)")
            } else {
                print("  \(a.name)  读不到  [\(a.note ?? "")]")
            }
        }
        let cs = inv.campaigns ?? []
        if !cs.isEmpty {
            print("活动（近 30 天，共 \(cs.count) 条）：")
            for c in cs.suffix(5) { print("  \(c.found_at) \(c.source)：\(c.text)") }
        }
    } else {
        print("积分盘查：还没有数据")
    }
    exit(0)
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
